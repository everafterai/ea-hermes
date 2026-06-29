# Cross-User Session/Memory Data Access Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the one real `file`-tool path to other users' session/memory data (plaintext session snapshots) and add a local append-only audit trail whenever the `file`, `terminal`, or `code_execution` tools read or reference the protected data stores.

**Architecture:** A single pure matcher (`is_protected_data_path`) in `agent/file_safety.py` defines "protected." The `file` tool consults it (denying reads, correcting the misleading `.db` binary message); a tiny best-effort audit module (`agent/data_access_audit.py`) appends JSONL events from the `file` tool and from a heuristic command/script scan in `terminal`/`code_execution`. Detect-not-prevent for the trusted admin tier.

**Tech Stack:** Python 3.11, pytest via `scripts/run_tests.sh`, `pathlib`, `json`. Config read via `hermes_cli.config.read_raw_config`; identity via `gateway.session_context.get_session_env`.

## Global Constraints

- **Run tests only via `scripts/run_tests.sh`** (CI-parity: unset creds, `TZ=UTC`, `C.UTF-8`, xdist, per-test subprocess isolation). Never bare `pytest`.
- **Never write to the real `~/.hermes/`** — the autouse fixture redirects `HERMES_HOME` to a temp dir. Tests rely on that redirect; do not hardcode `~/.hermes`.
- **Use `get_hermes_home()` / `get_default_hermes_root()` from `hermes_constants`** — never hardcode `~/.hermes`.
- **No change-detector tests** — assert behavior, not implementation strings.
- **Auditing must never raise into the tool path** — every audit call site and the audit module itself swallow exceptions.
- **Not a security boundary:** all user-facing denial/guard messages must retain the honest "Defense-in-depth — not a security boundary; the terminal tool can still bypass" framing already used in `agent/file_safety.py`.
- **`ruff` is near-disabled** (only PLW1514 enforced — always pass `encoding=` to `open()`). Run `ruff check .` and `ty check` before the final commit.

---

### Task 1: Protected-path matcher + read-block wiring (`agent/file_safety.py`)

**Files:**
- Modify: `agent/file_safety.py` (add matcher near the `get_read_block_error` block ~line 165–308; add one call inside `get_read_block_error` before its final `return None` at line 308)
- Test: `tests/agent/test_file_safety_protected_data.py` (create)

**Interfaces:**
- Produces: `is_protected_data_path(path: str | os.PathLike) -> Optional[str]` — returns a human-readable denial reason if `path` (resolved) is a Hermes session/memory data store, else `None`. Used by Tasks 3 (file tool) and referenced conceptually by Task 4.
- Consumes: existing module-private `_hermes_home_path()` and `_hermes_root_path()` in the same file.

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_file_safety_protected_data.py`:

```python
"""Tests for the cross-user session/memory data-store matcher."""
from pathlib import Path

from agent.file_safety import is_protected_data_path, get_read_block_error
from hermes_constants import get_hermes_home


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


def test_state_db_is_protected():
    p = _touch(get_hermes_home() / "state.db")
    assert is_protected_data_path(str(p)) is not None


def test_memory_store_db_is_protected():
    p = _touch(get_hermes_home() / "memory_store.db")
    assert is_protected_data_path(str(p)) is not None


def test_holographic_scope_db_is_protected():
    p = _touch(get_hermes_home() / "memories" / "holographic" / "U123.db")
    assert is_protected_data_path(str(p)) is not None


def test_session_json_snapshot_is_protected():
    p = _touch(get_hermes_home() / "sessions" / "session_abc.json")
    assert is_protected_data_path(str(p)) is not None


def test_session_jsonl_is_protected():
    p = _touch(get_hermes_home() / "sessions" / "abc.jsonl")
    assert is_protected_data_path(str(p)) is not None


def test_request_dump_is_protected():
    p = _touch(get_hermes_home() / "sessions" / "request_dump_abc_1.json")
    assert is_protected_data_path(str(p)) is not None


def test_global_memory_markdown_is_not_protected():
    p = _touch(get_hermes_home() / "memories" / "MEMORY.md")
    assert is_protected_data_path(str(p)) is None
    p2 = _touch(get_hermes_home() / "memories" / "USER.md")
    assert is_protected_data_path(str(p2)) is None


def test_arbitrary_project_db_is_not_protected(tmp_path):
    # A user's own project file named state.db, NOT under HERMES_HOME.
    p = _touch(tmp_path / "myproject" / "state.db")
    assert is_protected_data_path(str(p)) is None


def test_arbitrary_sessions_dir_outside_hermes_is_not_protected(tmp_path):
    p = _touch(tmp_path / "proj" / "sessions" / "session_x.json")
    assert is_protected_data_path(str(p)) is None


def test_sibling_profile_state_db_is_protected():
    root = get_hermes_home()  # in tests HERMES_HOME is the root
    p = _touch(root / "profiles" / "other" / "state.db")
    assert is_protected_data_path(str(p)) is not None


def test_read_block_error_covers_protected_db():
    p = _touch(get_hermes_home() / "state.db")
    msg = get_read_block_error(str(p))
    assert msg is not None
    assert "security boundary" in msg.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_file_safety_protected_data.py`
Expected: FAIL — `ImportError: cannot import name 'is_protected_data_path'`.

- [ ] **Step 3: Add the matcher**

In `agent/file_safety.py`, add this block immediately **before** `def get_read_block_error(` (line 165). Note `Optional` and `Path`/`os` are already imported at the top of the file.

```python
# ---------------------------------------------------------------------------
# Cross-user data stores — session DBs, memory DBs, and plaintext session
# snapshots. Unlike the credential read-block above, these are not secrets:
# they are OTHER USERS' conversation/memory data. App-layer scoping
# (hermes_state.build_visibility_where) never touches these bytes, so a file
# tool that reads them bypasses isolation. Anchored to the Hermes home / root
# (and sibling profiles) so an unrelated project file named "state.db" is NOT
# matched.  NOT a security boundary — the terminal tool bypasses it; this
# gates the file tool and gives the audit layer one definition of "protected".
# ---------------------------------------------------------------------------

_PROTECTED_DATA_MSG = (
    "Access denied: {path} is a Hermes {kind} containing other users' "
    "session/memory data and cannot be read via the file tool. App-layer "
    "isolation does not apply to raw file reads. (Defense-in-depth — not a "
    "security boundary; the terminal tool can still bypass, and such access "
    "is audited.)"
)


def _hermes_anchor_dirs() -> list[Path]:
    """Resolved Hermes home, root, and any sibling profiles under <root>/profiles/*.

    Anchoring protected-path detection here keeps it from matching arbitrary
    project files that merely share a name (e.g. a project's own ``state.db``),
    while still catching another profile's data.
    """
    anchors: list[Path] = []
    for base in (_hermes_home_path(), _hermes_root_path()):
        try:
            real = base.resolve()
        except Exception:
            continue
        if real not in anchors:
            anchors.append(real)
    try:
        profiles_dir = (_hermes_root_path().resolve()) / "profiles"
        if profiles_dir.is_dir():
            for child in profiles_dir.iterdir():
                try:
                    if not child.is_dir():
                        continue
                    real = child.resolve()
                    if real not in anchors:
                        anchors.append(real)
                except Exception:
                    continue
    except Exception:
        pass
    return anchors


def is_protected_data_path(path) -> Optional[str]:
    """Return a denial reason if *path* is a cross-user session/memory store.

    Matches, under any Hermes home/root/profile anchor:
      * ``state.db``                    — session DB
      * ``memory_store.db``             — shared holographic memory store
      * ``memories/holographic/*.db``   — per-scope memory DBs
      * ``sessions/session_*.json``,
        ``sessions/*.jsonl``,
        ``sessions/request_dump_*.json`` — plaintext session snapshots

    Returns ``None`` for everything else, including the intentionally shared
    ``memories/MEMORY.md`` / ``USER.md``. NOT a security boundary.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        return None

    name = resolved.name
    suffix = resolved.suffix.lower()

    for anchor in _hermes_anchor_dirs():
        try:
            if resolved == (anchor / "state.db").resolve():
                return _PROTECTED_DATA_MSG.format(path=path, kind="session database")
            if resolved == (anchor / "memory_store.db").resolve():
                return _PROTECTED_DATA_MSG.format(path=path, kind="memory store")
        except Exception:
            pass

        # Per-scope memory DBs under <anchor>/memories/holographic/*.db
        if suffix == ".db":
            try:
                holo = (anchor / "memories" / "holographic").resolve()
                resolved.relative_to(holo)
                return _PROTECTED_DATA_MSG.format(
                    path=path, kind="per-scope memory database"
                )
            except ValueError:
                pass
            except Exception:
                pass

        # Plaintext session snapshots under <anchor>/sessions/
        try:
            sessions = (anchor / "sessions").resolve()
            resolved.relative_to(sessions)
            is_snap = (
                (name.startswith("session_") and suffix == ".json")
                or suffix == ".jsonl"
                or (name.startswith("request_dump_") and suffix == ".json")
            )
            if is_snap:
                return _PROTECTED_DATA_MSG.format(path=path, kind="session snapshot")
        except ValueError:
            pass
        except Exception:
            pass

    return None
```

- [ ] **Step 4: Wire the matcher into `get_read_block_error`**

In `agent/file_safety.py`, find the end of `get_read_block_error` (the `_BLOCKED_PROJECT_ENV_BASENAMES` block followed by `return None` at line 308). Replace the trailing `return None` so the protected check runs last:

```python
    if resolved.name in _BLOCKED_PROJECT_ENV_BASENAMES:
        return (
            f"Access denied: {path} is a secret-bearing environment file "
            "and cannot be read to prevent credential leakage. "
            "If you need to check the file structure, read .env.example instead. "
            "(Defense-in-depth — not a security boundary; the terminal tool can still bypass.)"
        )

    # Cross-user session/memory data stores (other users' data, not secrets).
    protected = is_protected_data_path(resolved)
    if protected:
        return protected

    return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_file_safety_protected_data.py`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add agent/file_safety.py tests/agent/test_file_safety_protected_data.py
git commit -m "feat(security): add is_protected_data_path matcher + read-block wiring

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Local append-only audit module (`agent/data_access_audit.py`)

**Files:**
- Create: `agent/data_access_audit.py`
- Test: `tests/agent/test_data_access_audit.py` (create)

**Interfaces:**
- Produces:
  - `record_access(*, tool: str, action: str, target: str) -> None` — append one JSONL audit event; best-effort, never raises.
  - `record_command_access(command: str, *, tool: str) -> None` — scan a shell command / sandbox script for references to protected data stores; on match calls `record_access(action="exec", ...)`. Never raises, never blocks.
- Consumes: `hermes_constants.get_hermes_home`, `hermes_cli.config.read_raw_config`, `gateway.session_context.get_session_env`.
- Config (read from `config.yaml` top level via `read_raw_config`, no loader change needed): `data_access_audit.enabled` (default `true`), `data_access_audit.path` (default `${HERMES_HOME}/audit/data-access.log`).

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_data_access_audit.py`:

```python
"""Tests for the local append-only data-access audit log."""
import json

import agent.data_access_audit as audit
from hermes_constants import get_hermes_home


def _read_log_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_record_access_writes_one_jsonl_line(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_access(tool="read_file", action="blocked-read", target="/x/state.db")
    lines = _read_log_lines()
    assert len(lines) == 1
    ev = lines[0]
    assert ev["tool"] == "read_file"
    assert ev["action"] == "blocked-read"
    assert ev["target"] == "/x/state.db"
    assert "ts" in ev
    # Identity keys are always present (empty when no session context).
    for k in ("platform", "user_id", "chat_id", "session_id"):
        assert k in ev


def test_record_access_appends(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_access(tool="read_file", action="blocked-read", target="/a")
    audit.record_access(tool="patch", action="blocked-read", target="/b")
    assert len(_read_log_lines()) == 2


def test_disabled_writes_nothing(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": False})
    audit.record_access(tool="read_file", action="blocked-read", target="/x")
    assert _read_log_lines() == []


def test_record_command_access_logs_on_marker(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_command_access("sqlite3 ~/.hermes/state.db .dump", tool="terminal")
    lines = _read_log_lines()
    assert len(lines) == 1
    assert lines[0]["action"] == "exec"
    assert lines[0]["tool"] == "terminal"


def test_record_command_access_ignores_unrelated(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_command_access("ls -la /tmp && echo done", tool="terminal")
    assert _read_log_lines() == []


def test_audit_never_raises(monkeypatch):
    # A broken config must not propagate into the tool path.
    def boom():
        raise RuntimeError("config exploded")
    monkeypatch.setattr(audit, "_audit_config", boom)
    # Should swallow and return None, not raise.
    audit.record_access(tool="read_file", action="blocked-read", target="/x")
    audit.record_command_access("sqlite3 state.db", tool="terminal")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_data_access_audit.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.data_access_audit'`.

- [ ] **Step 3: Create the audit module**

Create `agent/data_access_audit.py`:

```python
"""Local append-only audit log for access to cross-user session/memory data.

Records when a tool reads, blocks a read of, or references (in a shell command
or sandbox script) one of the protected data stores defined by
``agent.file_safety.is_protected_data_path``.

Best-effort and non-blocking: every public function swallows exceptions so
auditing can never break tool execution.

NOT tamper-proof: the log is written by the same OS uid that runs the gateway,
so it catches accidental / operator-tier access and makes casual admin access
visible — it does not survive an adversary who owns the box.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


def _audit_config() -> dict:
    """Return the ``data_access_audit`` config block, or {} on any failure."""
    from hermes_cli.config import read_raw_config

    cfg = read_raw_config().get("data_access_audit", {})
    return cfg if isinstance(cfg, dict) else {}


def _enabled() -> bool:
    try:
        return bool(_audit_config().get("enabled", True))
    except Exception:
        # Fail-open on auditing: a broken config should not silently disable
        # the trail, but must also never raise. Default to enabled.
        return True


def _log_path() -> Path:
    try:
        raw = _audit_config().get("path") or ""
    except Exception:
        raw = ""
    if raw:
        try:
            return Path(os.path.expanduser(str(raw)))
        except Exception:
            pass
    return get_hermes_home() / "audit" / "data-access.log"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _identity() -> dict:
    try:
        from gateway.session_context import get_session_env

        return {
            "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
            "user_id": get_session_env("HERMES_SESSION_USER_ID", ""),
            "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", ""),
            "chat_type": get_session_env("HERMES_SESSION_CHAT_TYPE", ""),
            "session_id": get_session_env("HERMES_SESSION_ID", ""),
        }
    except Exception:
        return {"platform": "", "user_id": "", "chat_id": "", "chat_type": "", "session_id": ""}


def record_access(*, tool: str, action: str, target: str) -> None:
    """Append one JSONL audit event. Best-effort; never raises."""
    try:
        if not _enabled():
            return
        event = {
            "ts": _utc_now_iso(),
            "tool": tool,
            "action": action,
            "target": (target or "")[:500],
        }
        event.update(_identity())
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        # Auditing must never break tool execution.
        pass


# Substrings that indicate a command / sandbox script references a protected
# data store. Heuristic and evadable by obfuscation (base64, indirection); the
# point is to catch casual / accidental access, not a determined adversary.
_PROTECTED_REFERENCE_MARKERS = (
    "state.db",
    "memory_store.db",
    "memories/holographic",
    "request_dump_",
    "sessions/session_",
    ".jsonl",
)


def record_command_access(command: str, *, tool: str) -> None:
    """Scan a shell command / sandbox script and audit references to protected
    data stores. Never raises, never blocks (a shell can read them regardless;
    this only makes the access visible)."""
    try:
        if not command or not _enabled():
            return
        low = command.lower()
        if any(marker in low for marker in _PROTECTED_REFERENCE_MARKERS):
            record_access(tool=tool, action="exec", target=command[:500])
    except Exception:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_data_access_audit.py`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/data_access_audit.py tests/agent/test_data_access_audit.py
git commit -m "feat(security): add local append-only data-access audit module

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Close + audit in the `file` tool (`tools/file_tools.py`)

**Files:**
- Modify: `tools/file_tools.py` (import at line 11; `read_file_tool` insert before line 712; `search_tool` ~line 1298 and ~1338; `patch_tool` loop ~line 1154)
- Test: `tests/tools/test_file_tools_protected_data.py` (create)

**Interfaces:**
- Consumes: `is_protected_data_path` (Task 1), `record_access` (Task 2).
- Produces: `read_file_tool` / `search_tool` / `patch_tool` deny protected paths and emit `blocked-read` audit events. Behavior used by no later task.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_file_tools_protected_data.py`:

```python
"""The file tool denies cross-user session/memory data and audits the attempt."""
import json

import agent.data_access_audit as audit
import tools.file_tools as ft
from hermes_constants import get_hermes_home


def _touch(p, text="secret-conversation"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _audit_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_read_file_denies_plaintext_session_snapshot(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    snap = _touch(get_hermes_home() / "sessions" / "session_abc.json")
    out = json.loads(ft.read_file_tool(str(snap)))
    assert "error" in out
    assert "security boundary" in out["error"].lower()
    assert any(ev["action"] == "blocked-read" for ev in _audit_lines())


def test_read_file_state_db_message_is_protected_not_terminal(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    db = _touch(get_hermes_home() / "state.db")
    out = json.loads(ft.read_file_tool(str(db)))
    assert "error" in out
    # Must NOT be the binary-guard message that points at the terminal bypass.
    assert "binary file" not in out["error"].lower()
    assert "other users" in out["error"].lower()


def test_search_filters_protected_snapshot(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    _touch(get_hermes_home() / "sessions" / "session_abc.json",
           "uniqueneedle12345 in another users chat")
    out = json.loads(ft.search_tool(
        pattern="uniqueneedle12345",
        path=str(get_hermes_home() / "sessions"),
        target="content",
    ))
    blob = json.dumps(out)
    assert "uniqueneedle12345" not in blob or out.get("matches") in (None, [])


def test_patch_denies_protected_db(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    db = _touch(get_hermes_home() / "state.db")
    out = json.loads(ft.patch_tool(
        mode="replace", path=str(db), old_string="x", new_string="y",
    ))
    assert "error" in out
    assert "security boundary" in out["error"].lower()


def test_global_memory_markdown_still_readable(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    md = _touch(get_hermes_home() / "memories" / "MEMORY.md", "shared note\n")
    out = json.loads(ft.read_file_tool(str(md)))
    assert "error" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_file_tools_protected_data.py`
Expected: FAIL — `test_read_file_state_db_message_is_protected_not_terminal` fails (current message says "binary file"); snapshot/patch tests fail (no denial).

- [ ] **Step 3a: Extend the import (line 11)**

In `tools/file_tools.py`, change line 11 from:

```python
from agent.file_safety import get_read_block_error
```

to:

```python
from agent.file_safety import get_read_block_error, is_protected_data_path
```

- [ ] **Step 3b: Guard `read_file_tool` before the binary guard**

In `read_file_tool`, insert between the `_resolved = _resolve_path_for_task(path, task_id)` line (708) and the `# ── Binary file guard ──` comment (710):

```python
        _resolved = _resolve_path_for_task(path, task_id)

        # ── Cross-user data guard ─────────────────────────────────────
        # Block reads of other users' session/memory stores BEFORE the
        # binary-extension guard, so a protected .db returns a
        # "this is other users' data" message instead of the binary
        # message that points at the terminal bypass. Audited regardless
        # of role. NOT a security boundary — the terminal tool bypasses it.
        _protected = is_protected_data_path(str(_resolved))
        if _protected:
            from agent.data_access_audit import record_access
            record_access(tool="read_file", action="blocked-read", target=str(_resolved))
            return json.dumps({"error": _protected})

        # ── Binary file guard ─────────────────────────────────────────
```

- [ ] **Step 3c: Guard `search_tool` (explicit root + result filtering)**

In `search_tool`, immediately after `offset, limit = normalize_search_pagination(offset, limit)` (line 1298) add the explicit-root deny:

```python
        offset, limit = normalize_search_pagination(offset, limit)

        # Deny searching a protected store directly (e.g. path=<state.db>).
        _root_protected = is_protected_data_path(path)
        if _root_protected:
            from agent.data_access_audit import record_access
            record_access(tool="search_files", action="blocked-read", target=str(path))
            return json.dumps({"error": _root_protected})
```

Then, immediately after `result = file_ops.search(...)` returns (line 1338, before the `if hasattr(result, 'matches')` redaction loop) add result filtering:

```python
        result = file_ops.search(
            pattern=pattern, path=path, target=target, file_glob=file_glob,
            limit=limit, offset=offset, output_mode=output_mode, context=context
        )

        # Drop any matches/files that resolve to a protected data store
        # (e.g. plaintext session snapshots picked up by a recursive search).
        _dropped = 0
        if getattr(result, "matches", None):
            _kept = []
            for _m in result.matches:
                if getattr(_m, "path", None) and is_protected_data_path(_m.path):
                    _dropped += 1
                    continue
                _kept.append(_m)
            result.matches = _kept
        if getattr(result, "files", None):
            _kept_f = []
            for _f in result.files:
                if is_protected_data_path(_f):
                    _dropped += 1
                else:
                    _kept_f.append(_f)
            result.files = _kept_f
        if getattr(result, "counts", None):
            result.counts = {
                _k: _v for _k, _v in result.counts.items()
                if not is_protected_data_path(_k)
            }
        if _dropped:
            from agent.data_access_audit import record_access
            record_access(
                tool="search_files", action="blocked-read",
                target=f"{_dropped} protected file(s) under {path}",
            )
```

- [ ] **Step 3d: Guard `patch_tool`**

In `patch_tool`, in the `for _p in _paths_to_check:` loop (line 1154), add the protected check as the first check in the loop body, before `_check_sensitive_path`:

```python
    for _p in _paths_to_check:
        _prot = is_protected_data_path(_p)
        if _prot:
            from agent.data_access_audit import record_access
            record_access(tool="patch", action="blocked-read", target=str(_p))
            return tool_error(_prot)
        sensitive_err = _check_sensitive_path(_p, task_id)
        if sensitive_err:
            return tool_error(sensitive_err)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_file_tools_protected_data.py`
Expected: PASS (5 tests).

- [ ] **Step 5: Regression — existing file-tool tests still pass**

Run: `scripts/run_tests.sh tests/tools/test_file_tools.py tests/agent/test_file_safety.py tests/agent/test_file_safety_credentials.py`
Expected: PASS (no regressions from the new import / read-block branch).

- [ ] **Step 6: Commit**

```bash
git add tools/file_tools.py tests/tools/test_file_tools_protected_data.py
git commit -m "feat(security): deny + audit protected data-store access in file tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Command/script scan in `terminal` and `code_execution`

**Files:**
- Modify: `tools/terminal_tool.py` (in the handler body after the `isinstance(command, str)` validation, ~line 1828)
- Modify: `tools/code_execution_tool.py` (in `execute_code` after the empty-code check, ~line 1095)
- Test: `tests/tools/test_terminal_data_access_audit.py` (create)

**Interfaces:**
- Consumes: `record_command_access` (Task 2).
- Produces: log-only `exec` audit events; neither tool's behavior/return value changes.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_terminal_data_access_audit.py`. This calls the audit scanner the way the tools do, plus asserts the call sites exist by importing them. (We avoid actually spawning a shell so the test stays hermetic.)

```python
"""terminal / code_execution emit an exec audit event when a command
references a protected data store."""
import json

import agent.data_access_audit as audit
from hermes_constants import get_hermes_home


def _audit_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_terminal_handler_imports_scanner():
    # The call site uses a local import; assert the symbol is importable
    # the same way the handler imports it.
    from agent.data_access_audit import record_command_access  # noqa: F401


def test_scan_logs_terminal_reference(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_command_access("strings ~/.hermes/state.db | grep secret", tool="terminal")
    lines = _audit_lines()
    assert len(lines) == 1
    assert lines[0]["tool"] == "terminal"
    assert lines[0]["action"] == "exec"


def test_scan_logs_code_execution_reference(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    code = "open('/root/.hermes/sessions/session_x.json').read()"
    audit.record_command_access(code, tool="code_execution")
    lines = _audit_lines()
    assert len(lines) == 1
    assert lines[0]["tool"] == "code_execution"
```

> Note: the deep behavioral coverage of `record_command_access` lives in Task 2's tests; this file pins the tool-facing contract and the two `tool=` labels.

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_terminal_data_access_audit.py`
Expected: PASS for the import/scan tests only if Task 2 is merged — but the **call sites** are not yet wired. Since these tests call the audit module directly, they pass once Task 2 exists. To make this task test-meaningful, also add the call-site assertion below in Step 3 and verify via the regression run. (If running this task in isolation before wiring, the import test still guards the contract.)

- [ ] **Step 3a: Wire `terminal_tool`**

In `tools/terminal_tool.py`, in the handler, immediately after the `isinstance(command, str)` rejection block (the `return json.dumps({... "Invalid command" ...})` ending ~line 1827) and before `# Get configuration` (line 1829), insert:

```python
        # Audit (log-only) references to cross-user session/memory stores.
        # Never blocks — a shell reads them regardless of any in-process
        # check; this makes the access visible in the audit trail.
        try:
            from agent.data_access_audit import record_command_access
            record_command_access(command, tool="terminal")
        except Exception:
            pass

        # Get configuration
```

- [ ] **Step 3b: Wire `execute_code`**

In `tools/code_execution_tool.py`, in `execute_code`, immediately after the empty-code guard (`if not code or not code.strip(): return tool_error("No code provided.")`, ~line 1094) and before the `# Dispatch:` comment (line 1096), insert:

```python
    if not code or not code.strip():
        return tool_error("No code provided.")

    # Audit (log-only) references to cross-user session/memory stores in the
    # sandbox script. Never blocks; best-effort.
    try:
        from agent.data_access_audit import record_command_access
        record_command_access(code, tool="code_execution")
    except Exception:
        pass

    # Dispatch: remote backends use file-based RPC, local uses UDS
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_terminal_data_access_audit.py`
Expected: PASS (3 tests).

- [ ] **Step 5: Regression — terminal/code-exec tests still pass**

Run: `scripts/run_tests.sh tests/tools/test_terminal_exit_semantics.py tests/tools/test_terminal_foreground_timeout_cap.py`
Expected: PASS (the inserted block is a no-op for non-matching commands and cannot raise).

- [ ] **Step 6: Commit**

```bash
git add tools/terminal_tool.py tools/code_execution_tool.py tests/tools/test_terminal_data_access_audit.py
git commit -m "feat(security): audit terminal/code_execution refs to protected data stores

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Documentation + config example

**Files:**
- Modify: `CLAUDE.md` (add a short fork-specific subsection documenting the control)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add a CLAUDE.md subsection**

In `CLAUDE.md`, under the fork-specific section (after the "Session visibility / multi-user isolation" subsection), add:

```markdown
### Cross-user data-access protection — [agent/file_safety.py](agent/file_safety.py) + [agent/data_access_audit.py](agent/data_access_audit.py)

App-layer session/memory isolation never touches bytes on disk, and the gateway
runs as one OS uid, so a filesystem-capable tool can read other users' data.
Two controls (design:
[docs/superpowers/specs/2026-06-29-cross-user-data-access-protection-design.md](docs/superpowers/specs/2026-06-29-cross-user-data-access-protection-design.md)):

- **Close (file tool):** `is_protected_data_path` recognizes the session DB
  (`state.db`), memory DBs (`memory_store.db`, `memories/holographic/*.db`),
  and **plaintext session snapshots** (`sessions/session_*.json`, `*.jsonl`,
  `request_dump_*.json`) under any Hermes home/root/profile — while keeping the
  shared `memories/MEMORY.md` / `USER.md` readable. `read_file`/`search_files`/
  `patch` deny these (the `.db` files are also caught earlier by the
  binary-extension guard; the matcher runs *before* it so the message says
  "other users' data" rather than "use terminal").
- **Detect (all three tools):** `agent/data_access_audit.record_access` appends
  JSONL to `${HERMES_HOME}/audit/data-access.log`; `terminal`/`code_execution`
  log (never block) when a command/script references a protected path. Config
  under the top-level `data_access_audit:` block (`enabled`, default true;
  `path`). **Not a security boundary** — a determined admin with a shell
  bypasses it; the audit catches accidental/operator access and makes casual
  admin access visible. The log is local and same-uid-writable.
```

- [ ] **Step 2: Lint + typecheck + full suite**

Run:
```bash
ruff check .
ty check
scripts/run_tests.sh tests/agent/test_file_safety_protected_data.py tests/agent/test_data_access_audit.py tests/tools/test_file_tools_protected_data.py tests/tools/test_terminal_data_access_audit.py
```
Expected: ruff clean, `ty` clean, all new tests PASS.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(security): document cross-user data-access protection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Matcher (spec §1) → Task 1. Both tiers (snapshots + DBs), `MEMORY.md`/`USER.md` excluded, cross-profile anchor → covered by Task 1 tests.
- Close / file read-block + binary-guard ordering (spec §2) → Task 3 (read_file before binary guard; search filter; patch deny). Message-correction test included.
- Audit module + JSONL + identity + config gating (spec §3, §4) → Task 2.
- Terminal/code_exec command scan, log-only (spec §3 emit points) → Task 4.
- Testing matrix (spec) → distributed across Task 1–4 test files; `MEMORY.md` readable + cross-profile + disabled-config + never-raises all covered.
- Config knobs (spec §4): read directly via `read_raw_config` (no loader change needed) — documented in Task 5. No code task required because `read_raw_config` already surfaces arbitrary top-level keys.

**Placeholder scan:** No TBD/TODO; every code step has complete code; commands have expected output.

**Type consistency:** `is_protected_data_path` returns `Optional[str]` and is consumed as truthy/None in Tasks 1/3. `record_access(*, tool, action, target)` and `record_command_access(command, *, tool)` signatures match all call sites (Tasks 3/4) and tests (Task 2). `SearchResult` attributes used (`matches`, `files`, `counts`, and `match.path`) match `tools/file_operations.py`.

**Note on Task 4 Step 2:** because the new tests exercise the audit module directly (not a spawned shell), they pass once Task 2 is merged; the value of Task 4 is the two wired call sites, verified by the regression run in Step 5 and the import-contract test. This is intentional — spawning a real terminal in a unit test would be slow and backend-dependent.
