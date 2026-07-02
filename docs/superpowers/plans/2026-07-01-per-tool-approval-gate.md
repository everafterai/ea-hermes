# Per-Tool Approval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `~/.hermes/config.yaml` name tools (native or MCP) that require human approval before each invocation, block on that approval across CLI/TUI/Slack, authorize it in cron via explicit acknowledgment, and roll it out to gate Stripe write tools over the hosted Stripe MCP server.

**Architecture:** A new `check_tool_approval()` in `tools/approval.py` runs at the single `pre_tool_call` dispatch point in `model_tools.handle_function_call` (beside the existing RBAC backstop). It reuses the existing in-thread gateway-approval primitive (queue + per-session notify callback + `threading.Event`, all already in `approval.py`) so a gated tool blocks the agent thread and prompts the real user, exactly like the terminal dangerous-command guard — no conversation-loop changes. Cron has no user, so it skips only when the job owner has standing permission AND the tool was acknowledged at creation; otherwise it follows `approvals.cron_mode`.

**Tech Stack:** Python 3.11, `fnmatch` (stdlib), existing `tools/approval.py` gateway primitives, `gateway/tool_access.py` RBAC, `cron/scheduler.py` + `cron/rbac_ceiling.py`, `agent/data_access_audit.py`. Tests via `scripts/run_tests.sh` (CI-parity wrapper — never bare pytest).

## Global Constraints

- **Backward compatible / fail-inert:** absent `approvals.require_for_tools` → `check_tool_approval` returns `{"approved": True}` immediately; no new prompts, no audit writes, behavior byte-for-byte unchanged.
- **Orthogonal to `approvals.mode`:** the gate is independent of `mode`; `mode: off` does NOT disable `require_for_tools`.
- **No permanent allowlisting** of gated tools: always call `prompt_dangerous_approval(..., allow_permanent=False)` and never `approve_permanent` for `tool:` keys.
- **RBAC precedence:** the RBAC hard-deny (`denial_for_current_tool`) runs BEFORE the approval gate. Do not reorder.
- **Fail-open on internal error** in the gate itself (like the RBAC backstop): a resolution failure must not block a tool the user was allowed to run — except the cron branch, which fails CLOSED via `cron_mode` (deny default).
- **Approval key format:** `pattern_key = f"tool:{tool_name}"` everywhere.
- **Tests never write to `~/.hermes/`** (autouse fixture redirects `HERMES_HOME`); no change-detector tests.
- **Run tests only via** `scripts/run_tests.sh` (e.g. `scripts/run_tests.sh tests/tools/test_tool_approval.py`).

## File Structure

- `tools/approval.py` — **modify.** Add config reader, tool matcher, arg redaction, the generic gateway blocking helper, and `check_tool_approval()`. (Reuses existing `_ApprovalEntry`, `_gateway_queues`, `_gateway_notify_cbs`, `resolve_gateway_approval`, `submit_pending`, `approve_session`, `is_approved`, `prompt_dangerous_approval`, `_get_approval_config`, `_get_cron_approval_mode`, `get_current_session_key` — all already present.)
- `model_tools.py` — **modify** (~line 1061). Insert the `check_tool_approval` call after the RBAC backstop in `handle_function_call`.
- `cron/scheduler.py` — **modify** (~line 1616–1644 run setup, ~2049 teardown). Export the owner grant + `unattended_approved_tools` into the run context.
- `cron/tool_approval_context.py` — **create.** Small contextvar module the scheduler writes and `check_tool_approval` reads (avoids threading the `job` dict into `model_tools`).
- `tools/cronjob_tools.py` — **modify** (`_rbac_creation_error`, ~line 459, and the create/update payload builder ~line 446). Add `unattended_approved_tools` field + create-time ack rejection.
- `gateway/tool_access.py` — **modify** (line 48). Add `"stripe"` to the `operator` `BUILTIN_ROLES` frozenset.
- Tests: `tests/tools/test_tool_approval.py` (new), `tests/tools/test_cronjob_unattended_ack.py` (new), `tests/gateway/test_tool_access.py` (extend), `tests/cron/test_tool_approval_context.py` (new).
- Docs/config (Task 10): the VM `~/.hermes/config.yaml` recipe + a memory note (no repo code).

## Scope Check / Execution Phasing

The spec is one feature with a rollout; it splits cleanly into three phases that each leave working, tested software. Recommended execution order:

- **Phase A — interactive gate (Tasks 1–5):** gated tools prompt and block in CLI/TUI/Slack. Self-contained and shippable.
- **Phase B — cron authorization (Tasks 6–8):** cron skip-when-acknowledged + create-time ack. Depends on Phase A's `check_tool_approval`.
- **Phase C — Stripe rollout (Tasks 9–10):** RBAC operator grant + MCP wiring + memory note. Depends on Phase A's config.

---

### Task 1: Config reader + tool matcher

**Files:**
- Modify: `tools/approval.py`
- Test: `tests/tools/test_tool_approval.py`

**Interfaces:**
- Consumes: existing `_get_approval_config() -> dict` (reads the `approvals:` block).
- Produces: `tool_requires_approval(tool_name: str) -> bool`; `_get_require_for_tools() -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_tool_approval.py
import tools.approval as ap


def test_tool_requires_approval_matches_exact_and_glob(monkeypatch):
    monkeypatch.setattr(
        ap, "_get_approval_config",
        lambda: {"require_for_tools": ["stripe_api_write", "send_*"]},
    )
    assert ap.tool_requires_approval("stripe_api_write") is True
    assert ap.tool_requires_approval("send_message") is True
    assert ap.tool_requires_approval("SEND_MESSAGE") is True      # case-insensitive
    assert ap.tool_requires_approval("read_file") is False


def test_tool_requires_approval_inert_when_unset(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config", lambda: {})
    assert ap.tool_requires_approval("stripe_api_write") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k requires_approval`
Expected: FAIL with `AttributeError: module 'tools.approval' has no attribute 'tool_requires_approval'`

- [ ] **Step 3: Write minimal implementation**

Add near the other `_get_approval_*` helpers in `tools/approval.py` (after `_get_cron_approval_mode`, ~line 935). `fnmatch` is already imported at module top; if not, add `import fnmatch`.

```python
def _get_require_for_tools() -> list[str]:
    """Read the ``approvals.require_for_tools`` globs (lowercased). Empty when unset."""
    raw = _get_approval_config().get("require_for_tools") or []
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",")]
    return [str(g).strip().lower() for g in raw if str(g).strip()]


def tool_requires_approval(tool_name: str) -> bool:
    """True if *tool_name* matches any ``require_for_tools`` glob (case-insensitive)."""
    name = (tool_name or "").lower()
    return any(fnmatch.fnmatchcase(name, glob) for glob in _get_require_for_tools())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k requires_approval`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tools/approval.py tests/tools/test_tool_approval.py
git commit -m "feat(approval): config-driven require_for_tools matcher"
```

---

### Task 2: Redacted args summary

**Files:**
- Modify: `tools/approval.py`
- Test: `tests/tools/test_tool_approval.py`

**Interfaces:**
- Produces: `_redact_tool_args(args: dict, *, max_len: int = 200) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_redact_tool_args_hides_secrets_and_truncates():
    from tools.approval import _redact_tool_args
    out = _redact_tool_args({
        "method": "POST",
        "path": "v1/refunds",
        "api_key": "sk_live_deadbeef",
        "authorization": "Bearer x",
        "body": "y" * 500,
    })
    assert "method=POST" in out
    assert "path=v1/refunds" in out
    assert "sk_live_deadbeef" not in out and "api_key=<redacted>" in out
    assert "authorization=<redacted>" in out
    assert len(out) <= 200 + 40   # body value truncated, not the whole map dropped


def test_redact_tool_args_empty():
    from tools.approval import _redact_tool_args
    assert _redact_tool_args({}) == "(no arguments)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k redact`
Expected: FAIL with `ImportError: cannot import name '_redact_tool_args'`

- [ ] **Step 3: Write minimal implementation**

Add to `tools/approval.py` (below `tool_requires_approval`):

```python
_SENSITIVE_ARG_PATTERNS = ("*key*", "*token*", "*secret*", "*password*", "*authorization*")


def _redact_tool_args(args: dict, *, max_len: int = 200) -> str:
    """One-line, secret-redacted summary of tool args for an approval prompt."""
    if not isinstance(args, dict) or not args:
        return "(no arguments)"
    parts = []
    for key, value in args.items():
        k = str(key)
        if any(fnmatch.fnmatchcase(k.lower(), p) for p in _SENSITIVE_ARG_PATTERNS):
            parts.append(f"{k}=<redacted>")
            continue
        v = str(value)
        if len(v) > max_len:
            v = v[:max_len] + "…"
        parts.append(f"{k}={v}")
    return ", ".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k redact`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tools/approval.py tests/tools/test_tool_approval.py
git commit -m "feat(approval): redacted args summary for approval prompts"
```

---

### Task 3: Generic gateway blocking helper

**Files:**
- Modify: `tools/approval.py`
- Test: `tests/tools/test_tool_approval.py`

**Interfaces:**
- Consumes: existing module globals `_lock`, `_gateway_queues`, `_gateway_notify_cbs`, `_ApprovalEntry`, and `resolve_gateway_approval(session_key, choice)`.
- Produces: `_await_tool_gateway_decision(session_key: str, tool_name: str, description: str, timeout_seconds: int) -> str` returning `"once"`, `"session"`, or `"deny"`.

> This mirrors terminal's `_await_gateway_decision` but is tool-generic and lives in `approval.py` (where the queue/notify primitives already are). It queues an entry, invokes the session's notify callback to prompt the user, and blocks on the entry's event until `resolve_gateway_approval` sets it.

- [ ] **Step 1: Write the failing test**

```python
import threading
import tools.approval as ap


def test_await_tool_gateway_decision_blocks_then_resolves():
    sk = "sess-xyz"
    seen = {}
    ap.register_gateway_notify(sk, lambda data: seen.update(data))
    try:
        # Resolve from another thread shortly after the wait begins.
        t = threading.Timer(0.2, lambda: ap.resolve_gateway_approval(sk, "session"))
        t.start()
        choice = ap._await_tool_gateway_decision(sk, "stripe_api_write", "money movement", timeout_seconds=5)
        assert choice == "session"
        assert seen.get("tool_name") == "stripe_api_write"
        assert seen.get("description") == "money movement"
    finally:
        ap.unregister_gateway_notify(sk)


def test_await_tool_gateway_decision_times_out_to_deny():
    sk = "sess-timeout"
    ap.register_gateway_notify(sk, lambda data: None)
    try:
        assert ap._await_tool_gateway_decision(sk, "t", "d", timeout_seconds=0.1) == "deny"
    finally:
        ap.unregister_gateway_notify(sk)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k gateway_decision`
Expected: FAIL with `AttributeError: ... '_await_tool_gateway_decision'`

- [ ] **Step 3: Write minimal implementation**

Add to `tools/approval.py` (after `resolve_gateway_approval`, ~line 645). The `_ApprovalEntry` dataclass (line 580) wraps an approval dict and exposes `.event` (a `threading.Event`) and `.result` (str); confirm those attributes when implementing.

```python
def _await_tool_gateway_decision(session_key: str, tool_name: str,
                                 description: str, timeout_seconds: int) -> str:
    """Prompt the session's gateway user to approve a gated tool and BLOCK
    the calling (agent) thread until they answer or the timeout elapses.
    Returns 'once', 'session', or 'deny'."""
    approval_data = {
        "tool_name": tool_name,
        "description": description,
        "pattern_keys": [f"tool:{tool_name}"],
        "kind": "tool_approval",
    }
    entry = _ApprovalEntry(approval_data)
    with _lock:
        notify_cb = _gateway_notify_cbs.get(session_key)
        if notify_cb is None:
            return "deny"
        _gateway_queues.setdefault(session_key, []).append(entry)
    try:
        notify_cb(approval_data)
    except Exception as err:
        logger.error("tool approval notify_cb failed: %s", err, exc_info=True)
        with _lock:
            queue = _gateway_queues.get(session_key) or []
            if entry in queue:
                queue.remove(entry)
        return "deny"
    if entry.event.wait(timeout=timeout_seconds):
        return entry.result or "deny"
    # Timed out — remove our entry so a late resolve doesn't target a dead wait.
    with _lock:
        queue = _gateway_queues.get(session_key) or []
        if entry in queue:
            queue.remove(entry)
    return "deny"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k gateway_decision`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tools/approval.py tests/tools/test_tool_approval.py
git commit -m "feat(approval): generic in-thread gateway approval for tools"
```

---

### Task 4: `check_tool_approval` orchestrator (interactive + CLI + already-approved)

**Files:**
- Modify: `tools/approval.py`
- Test: `tests/tools/test_tool_approval.py`

**Interfaces:**
- Consumes: `tool_requires_approval` (Task 1), `_redact_tool_args` (Task 2), `_await_tool_gateway_decision` (Task 3), and existing `is_approved`, `approve_session`, `submit_pending`, `prompt_dangerous_approval`, `_get_approval_timeout`, `get_current_session_key`, `_is_gateway_approval_context` (line 133), `env_var_enabled`.
- Produces: `check_tool_approval(tool_name: str, args: dict, session_key: str) -> dict`. Result shapes:
  - `{"approved": True, "message": None}` — not gated / already approved / allowed.
  - `{"approved": False, "message": "BLOCKED: ..."}` — denied.
  - The cron branch (Task 6) returns one of the same two shapes.

> Cron is handled in Task 6; here, the cron/non-interactive fallback denies via `cron_mode` for the non-cron non-interactive case and leaves a clearly-marked hook (`_cron_tool_decision`) that Task 6 fills in.

- [ ] **Step 1: Write the failing test**

```python
import tools.approval as ap


def _gate_on(monkeypatch, tools_list=("gated_tool",)):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": list(tools_list)})


def test_check_tool_approval_not_gated(monkeypatch):
    _gate_on(monkeypatch)
    assert ap.check_tool_approval("read_file", {}, "s1") == {"approved": True, "message": None}


def test_check_tool_approval_cli_session_grants_and_sticks(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    calls = {"n": 0}

    def fake_prompt(command, description, timeout_seconds=None, allow_permanent=True, approval_callback=None):
        calls["n"] += 1
        assert allow_permanent is False          # gated tools never permanent
        return "session"

    monkeypatch.setattr(ap, "prompt_dangerous_approval", fake_prompt)
    first = ap.check_tool_approval("gated_tool", {"path": "v1/refunds"}, "s2")
    assert first == {"approved": True, "message": None}
    second = ap.check_tool_approval("gated_tool", {"path": "v1/refunds"}, "s2")
    assert second == {"approved": True, "message": None}
    assert calls["n"] == 1                         # session approval reused, no re-prompt


def test_check_tool_approval_cli_deny_blocks(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "prompt_dangerous_approval",
                        lambda *a, **k: "deny")
    res = ap.check_tool_approval("gated_tool", {}, "s3")
    assert res["approved"] is False and "BLOCKED" in res["message"]


def test_check_tool_approval_gateway_blocks_via_helper(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(ap, "_await_tool_gateway_decision",
                        lambda sk, tool, desc, timeout_seconds: "once")
    res = ap.check_tool_approval("gated_tool", {}, "s4")
    assert res == {"approved": True, "message": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k check_tool_approval`
Expected: FAIL with `AttributeError: ... 'check_tool_approval'`

- [ ] **Step 3: Write minimal implementation**

Add to `tools/approval.py`:

```python
def _denied_result(tool_name: str, reason: str) -> dict:
    return {
        "approved": False,
        "message": (
            f"BLOCKED: '{tool_name}' requires approval and it was not granted "
            f"({reason}). Do NOT retry — ask the user to approve it."
        ),
    }


def check_tool_approval(tool_name: str, args: dict, session_key: str) -> dict:
    """Gate *tool_name* behind human approval when it matches
    ``approvals.require_for_tools``. Blocks in-thread on interactive surfaces."""
    try:
        if not tool_requires_approval(tool_name):
            return {"approved": True, "message": None}

        pattern_key = f"tool:{tool_name}"
        if is_approved(session_key, pattern_key):
            return {"approved": True, "message": None}

        description = f"{tool_name}({_redact_tool_args(args)})"
        timeout = _get_approval_timeout()

        # Interactive gateway (Slack/TUI): block until the user answers.
        if _is_gateway_approval_context():
            submit_pending(session_key, {"tool_name": tool_name,
                                         "pattern_key": pattern_key,
                                         "description": description})
            choice = _await_tool_gateway_decision(session_key, tool_name, description, timeout)
        elif env_var_enabled("HERMES_INTERACTIVE"):
            choice = prompt_dangerous_approval(
                description, f"Tool '{tool_name}' requires approval",
                timeout_seconds=timeout, allow_permanent=False)
        else:
            # Headless: cron authorization (Task 6) or fail-closed via cron_mode.
            return _cron_tool_decision(tool_name, pattern_key, description)

        if choice == "session":
            approve_session(session_key, pattern_key)
            return {"approved": True, "message": None}
        if choice == "once":
            return {"approved": True, "message": None}
        return _denied_result(tool_name, "user denied")
    except Exception as err:  # fail-open on internal error (interactive paths)
        logger.debug("check_tool_approval error (fail-open): %s", err)
        return {"approved": True, "message": None}


def _cron_tool_decision(tool_name: str, pattern_key: str, description: str) -> dict:
    """Placeholder headless decision — replaced in Task 6. For now fail closed
    unless cron_mode==approve, matching the command-guard cron fallback."""
    if _get_cron_approval_mode() == "approve":
        return {"approved": True, "message": None}
    return _denied_result(tool_name, "no user present to approve (headless)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py -k check_tool_approval`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tools/approval.py tests/tools/test_tool_approval.py
git commit -m "feat(approval): check_tool_approval orchestrator (interactive + cli)"
```

---

### Task 5: Wire the gate into the dispatcher

**Files:**
- Modify: `model_tools.py` (~line 1061, immediately after the RBAC backstop block)
- Test: `tests/tools/test_tool_approval_dispatch.py` (new)

**Interfaces:**
- Consumes: `tools.approval.check_tool_approval` (Task 4), `tools.approval.get_current_session_key`.
- Produces: on denial, `handle_function_call` returns `json.dumps({"error": <message>, "status": "blocked"})` before executing the tool.

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_tool_approval_dispatch.py
import json
import model_tools
import tools.approval as ap


def test_dispatch_blocks_gated_tool_on_deny(monkeypatch):
    monkeypatch.setattr(ap, "check_tool_approval",
                        lambda name, args, sk: {"approved": False, "message": "BLOCKED: nope"})
    # read_file is a real, always-registered tool; the gate must fire before it runs.
    out = model_tools.handle_function_call("read_file", {"file_path": "/etc/hostname"})
    data = json.loads(out)
    assert data.get("status") == "blocked"
    assert "BLOCKED" in data.get("error", "")


def test_dispatch_allows_when_gate_approves(monkeypatch):
    seen = {"called": False}
    def fake_gate(name, args, sk):
        seen["called"] = True
        return {"approved": True, "message": None}
    monkeypatch.setattr(ap, "check_tool_approval", fake_gate)
    model_tools.handle_function_call("read_file", {"file_path": "/etc/hostname"})
    assert seen["called"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval_dispatch.py`
Expected: FAIL — first test's result has no `"status": "blocked"` (the gate isn't wired yet).

- [ ] **Step 3: Write minimal implementation**

In `model_tools.py`, immediately AFTER the RBAC backstop `try/except` (ends at line 1061, before the ACP edit-approval block at 1063), insert:

```python
        # Per-tool approval gate: block gated tools until the user confirms.
        # Runs AFTER the RBAC hard-deny (may-you) and before execution (confirm-you).
        try:
            from tools.approval import check_tool_approval, get_current_session_key
            _approval = check_tool_approval(
                function_name, function_args,
                get_current_session_key(default=session_id or ""),
            )
            if not _approval.get("approved", True):
                return json.dumps(
                    {"error": _approval.get("message", "Tool approval denied"),
                     "status": "blocked"},
                    ensure_ascii=False,
                )
        except Exception as _appr_err:
            logger.debug("per-tool approval gate error (fail-open): %s", _appr_err)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval_dispatch.py`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the broader suite for regressions, then commit**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py tests/tools/test_tool_approval_dispatch.py`
Expected: PASS

```bash
git add model_tools.py tests/tools/test_tool_approval_dispatch.py
git commit -m "feat(approval): enforce per-tool approval at the dispatch backstop"
```

---

### Task 6: Cron authorization branch

**Files:**
- Create: `cron/tool_approval_context.py`
- Modify: `tools/approval.py` (`_cron_tool_decision` from Task 4)
- Test: `tests/cron/test_tool_approval_context.py` (new), extend `tests/tools/test_tool_approval.py`

**Interfaces:**
- Produces (context module): `set_cron_tool_context(*, owner_grant: frozenset[str] | None, acked_tools: list[str]) -> object` (returns a reset token), `clear_cron_tool_context(token) -> None`, `get_cron_tool_context() -> tuple[frozenset[str] | None, frozenset[str]]` (grant, acked-tool-name set), and `in_cron_run() -> bool`.
- Consumes in `_cron_tool_decision`: `gateway.tool_access._toolset_for_tool(tool_name)` (resolves a tool name to its toolset, e.g. `stripe_api_write` → `mcp-stripe`), `agent.data_access_audit.record_access(tool=..., action=..., target=...)`.

- [ ] **Step 1: Write the failing test (context module)**

```python
# tests/cron/test_tool_approval_context.py
from cron.tool_approval_context import (
    set_cron_tool_context, clear_cron_tool_context,
    get_cron_tool_context, in_cron_run,
)


def test_cron_tool_context_roundtrip():
    assert in_cron_run() is False
    tok = set_cron_tool_context(owner_grant=frozenset({"mcp-stripe"}),
                                acked_tools=["stripe_api_write"])
    try:
        assert in_cron_run() is True
        grant, acked = get_cron_tool_context()
        assert grant == frozenset({"mcp-stripe"})
        assert acked == frozenset({"stripe_api_write"})
    finally:
        clear_cron_tool_context(tok)
    assert in_cron_run() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/cron/test_tool_approval_context.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'cron.tool_approval_context'`

- [ ] **Step 3: Write the context module**

```python
# cron/tool_approval_context.py
"""Per-run cron context for the per-tool approval gate.

The scheduler resolves the job owner's toolset grant and the job's
``unattended_approved_tools`` at run start and stashes them here so the
approval gate (running deep in model_tools) can authorize headless tool calls
without threading the job dict through the dispatcher.
"""
from __future__ import annotations

import contextvars
from typing import Optional, Tuple

_owner_grant: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "cron_tool_owner_grant", default=None)
_acked_tools: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "cron_tool_acked", default=None)
_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "cron_tool_active", default=False)


def set_cron_tool_context(*, owner_grant, acked_tools):
    t1 = _owner_grant.set(frozenset(owner_grant) if owner_grant is not None else None)
    t2 = _acked_tools.set(frozenset(acked_tools or ()))
    t3 = _active.set(True)
    return (t1, t2, t3)


def clear_cron_tool_context(token) -> None:
    t1, t2, t3 = token
    _owner_grant.reset(t1)
    _acked_tools.reset(t2)
    _active.reset(t3)


def get_cron_tool_context() -> Tuple[Optional[frozenset], frozenset]:
    return _owner_grant.get(), (_acked_tools.get() or frozenset())


def in_cron_run() -> bool:
    return bool(_active.get())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/cron/test_tool_approval_context.py`
Expected: PASS

- [ ] **Step 5: Write the failing test (cron decision in approval.py)**

```python
# add to tests/tools/test_tool_approval.py
import tools.approval as ap
from cron.tool_approval_context import set_cron_tool_context, clear_cron_tool_context


def test_cron_skips_when_acked_and_owner_has_grant(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"]})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "_toolset_for_tool", lambda name: "mcp-stripe")
    tok = set_cron_tool_context(owner_grant=frozenset({"mcp-stripe"}),
                                acked_tools=["stripe_api_write"])
    try:
        res = ap.check_tool_approval("stripe_api_write", {"method": "POST"}, "cronsess")
        assert res == {"approved": True, "message": None}
    finally:
        clear_cron_tool_context(tok)


def test_cron_denies_when_not_acked(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"], "cron_mode": "deny"})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "_toolset_for_tool", lambda name: "mcp-stripe")
    tok = set_cron_tool_context(owner_grant=frozenset({"mcp-stripe"}), acked_tools=[])
    try:
        res = ap.check_tool_approval("stripe_api_write", {}, "cronsess")
        assert res["approved"] is False
    finally:
        clear_cron_tool_context(tok)


def test_cron_denies_when_owner_lacks_grant(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"], "cron_mode": "deny"})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "_toolset_for_tool", lambda name: "mcp-stripe")
    tok = set_cron_tool_context(owner_grant=frozenset({"web"}), acked_tools=["stripe_api_write"])
    try:
        assert ap.check_tool_approval("stripe_api_write", {}, "cronsess")["approved"] is False
    finally:
        clear_cron_tool_context(tok)
```

- [ ] **Step 6: Replace `_cron_tool_decision` with the real logic**

In `tools/approval.py`, replace the Task-4 placeholder. Import `_toolset_for_tool` lazily (avoid a heavy import at module load). Add a `_toolset_for_tool` module-level reference for test monkeypatching.

```python
def _toolset_for_tool(tool_name: str):
    from gateway.tool_access import _toolset_for_tool as _impl
    return _impl(tool_name)


def _audit_cron_tool(tool_name: str, outcome: str) -> None:
    try:
        from agent.data_access_audit import record_access
        record_access(tool=tool_name, action="approval-gated-unattended", target=outcome)
    except Exception as err:
        logger.debug("cron approval audit failed: %s", err)


def _cron_tool_decision(tool_name: str, pattern_key: str, description: str) -> dict:
    """Headless decision for a gated tool. Skip only when the job owner holds
    the tool's toolset grant AND the tool was acknowledged at creation;
    otherwise follow approvals.cron_mode (deny by default)."""
    from cron.tool_approval_context import get_cron_tool_context, in_cron_run

    if in_cron_run():
        owner_grant, acked = get_cron_tool_context()
        toolset = _toolset_for_tool(tool_name)
        granted = owner_grant is not None and (
            "*" in owner_grant or toolset in owner_grant
        )
        if granted and tool_name in acked:
            _audit_cron_tool(tool_name, "skipped-authorized")
            return {"approved": True, "message": None}

    if _get_cron_approval_mode() == "approve":
        _audit_cron_tool(tool_name, "cron_mode-approve")
        return {"approved": True, "message": None}
    return _denied_result(tool_name, "no user present to approve (headless)")
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/tools/test_tool_approval.py tests/cron/test_tool_approval_context.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add cron/tool_approval_context.py tools/approval.py tests/cron/test_tool_approval_context.py tests/tools/test_tool_approval.py
git commit -m "feat(approval): cron skip-when-authorized branch + run context"
```

---

### Task 7: Populate the cron run context from the scheduler

**Files:**
- Modify: `cron/scheduler.py` (run setup ~line 1616–1644; teardown ~line 2049)
- Test: `tests/cron/test_scheduler_tool_context.py` (new)

**Interfaces:**
- Consumes: `cron.rbac_ceiling.cron_owner_grant(job) -> frozenset | None`, `cron.tool_approval_context.set_cron_tool_context/clear_cron_tool_context`, and the job's `unattended_approved_tools` list.

> The scheduler already sets `HERMES_CRON_SESSION` (line 1616) and session vars (1643) at run start and clears them (2049). Add the tool-context set/clear alongside these, using the SAME owner grant `_cron_enabled_toolsets_with_ceiling` already computes via `cron_owner_grant`.

- [ ] **Step 1: Write the failing test**

```python
# tests/cron/test_scheduler_tool_context.py
import cron.scheduler as sched
from cron.rbac_ceiling import cron_owner_grant  # noqa: F401 (patched via sched)


def test_scheduler_exports_owner_grant_and_acked(monkeypatch):
    captured = {}
    def fake_set(*, owner_grant, acked_tools):
        captured["grant"] = owner_grant
        captured["acked"] = acked_tools
        return "tok"
    monkeypatch.setattr(sched, "cron_owner_grant", lambda job: frozenset({"mcp-stripe"}))
    monkeypatch.setattr(sched, "set_cron_tool_context", fake_set)
    job = {"id": "j1", "unattended_approved_tools": ["stripe_api_write"]}
    tok = sched._enter_cron_tool_context(job)
    assert tok == "tok"
    assert captured["grant"] == frozenset({"mcp-stripe"})
    assert captured["acked"] == ["stripe_api_write"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/cron/test_scheduler_tool_context.py`
Expected: FAIL with `AttributeError: module 'cron.scheduler' has no attribute '_enter_cron_tool_context'`

- [ ] **Step 3: Add the helper and call sites in `cron/scheduler.py`**

Near the top-of-module imports add:

```python
from cron.rbac_ceiling import cron_owner_grant
from cron.tool_approval_context import set_cron_tool_context, clear_cron_tool_context
```

Add the helper (module scope):

```python
def _enter_cron_tool_context(job: dict):
    """Export the job owner's grant + acknowledged tools for the approval gate."""
    try:
        grant = cron_owner_grant(job)
    except Exception:
        grant = None
    acked = job.get("unattended_approved_tools") or []
    return set_cron_tool_context(owner_grant=grant, acked_tools=acked)
```

At run start, right after `os.environ["HERMES_CRON_SESSION"] = "1"` (line 1616):

```python
    _cron_tool_ctx_token = _enter_cron_tool_context(job)
```

At teardown, alongside `clear_session_vars(_ctx_tokens)` (line 2049):

```python
    clear_cron_tool_context(_cron_tool_ctx_token)
```

(Guard the teardown with `if '_cron_tool_ctx_token' in dir():`-style safety if the run can abort before setup; match the existing token-cleanup pattern in this function.)

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/cron/test_scheduler_tool_context.py`
Expected: PASS

- [ ] **Step 5: Run the cron suite for regressions, then commit**

Run: `scripts/run_tests.sh tests/cron/`
Expected: PASS

```bash
git add cron/scheduler.py tests/cron/test_scheduler_tool_context.py
git commit -m "feat(cron): export owner grant + acked tools for the approval gate"
```

---

### Task 8: Create-time acknowledgment gate

**Files:**
- Modify: `tools/cronjob_tools.py` (`_rbac_creation_error` ~line 459; payload builder ~line 446)
- Test: `tests/tools/test_cronjob_unattended_ack.py` (new)

**Interfaces:**
- Consumes: `tools.approval.tool_requires_approval` (Task 1), `gateway.tool_access._toolset_for_tool`, and the registry's `get_tool_names_for_toolset(toolset)` (`tools/registry.py:201`).
- Produces: an added `unattended_approved_tools` field on the persisted job; a creation/update rejection when a gated tool is callable-but-unacknowledged.

> "Callable" = the job's `enabled_toolsets` includes a toolset that contains a `require_for_tools`-matched tool. Resolve each such toolset's tool names via `get_tool_names_for_toolset`, match against `require_for_tools`, and require every match to appear in `unattended_approved_tools`.

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_cronjob_unattended_ack.py
import tools.cronjob_tools as cj


def test_unattended_ack_required_for_gated_tool(monkeypatch):
    # A job whose toolset makes a gated tool callable, but not acknowledged.
    monkeypatch.setattr("tools.approval.tool_requires_approval",
                        lambda name: name == "stripe_api_write")
    monkeypatch.setattr(cj, "_registry_tools_for_toolset",
                        lambda ts: ["stripe_api_write"] if ts == "mcp-stripe" else [])
    err = cj._unattended_ack_error(
        enabled_toolsets=["mcp-stripe"],
        unattended_approved_tools=[],
    )
    assert err is not None and "stripe_api_write" in err


def test_unattended_ack_satisfied(monkeypatch):
    monkeypatch.setattr("tools.approval.tool_requires_approval",
                        lambda name: name == "stripe_api_write")
    monkeypatch.setattr(cj, "_registry_tools_for_toolset",
                        lambda ts: ["stripe_api_write"] if ts == "mcp-stripe" else [])
    err = cj._unattended_ack_error(
        enabled_toolsets=["mcp-stripe"],
        unattended_approved_tools=["stripe_api_write"],
    )
    assert err is None


def test_unattended_ack_inert_when_no_gated_tool(monkeypatch):
    monkeypatch.setattr("tools.approval.tool_requires_approval", lambda name: False)
    monkeypatch.setattr(cj, "_registry_tools_for_toolset", lambda ts: ["read_file"])
    assert cj._unattended_ack_error(enabled_toolsets=["file"], unattended_approved_tools=[]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_cronjob_unattended_ack.py`
Expected: FAIL with `AttributeError: ... '_unattended_ack_error'`

- [ ] **Step 3: Implement the validator**

Add to `tools/cronjob_tools.py`:

```python
def _registry_tools_for_toolset(toolset: str) -> list[str]:
    try:
        from tools.registry import get_registry
        return get_registry().get_tool_names_for_toolset(toolset)
    except Exception:
        return []


def _unattended_ack_error(*, enabled_toolsets, unattended_approved_tools) -> Optional[str]:
    """Reject creation/update when a gated tool is callable but not acknowledged
    for unattended (cron) execution. Returns an error string, or None if OK."""
    from tools.approval import tool_requires_approval
    acked = set(unattended_approved_tools or [])
    unacked = []
    for toolset in (enabled_toolsets or []):
        for tool_name in _registry_tools_for_toolset(toolset):
            if tool_requires_approval(tool_name) and tool_name not in acked:
                unacked.append(tool_name)
    if unacked:
        names = ", ".join(sorted(set(unacked)))
        return (
            f"This job can call approval-gated tool(s) [{names}] but runs "
            "unattended. Acknowledge unattended execution by adding them to "
            "'unattended_approved_tools', or remove the toolset that exposes them."
        )
    return None
```

Then call it inside the create/update path where `_rbac_creation_error` is invoked, before the job is persisted (mirror how `_rbac_creation_error`'s return is handled), and add `unattended_approved_tools` to the persisted payload alongside `enabled_toolsets` (~line 450):

```python
    if job.get("unattended_approved_tools"):
        result["unattended_approved_tools"] = job["unattended_approved_tools"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_cronjob_unattended_ack.py`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tools/cronjob_tools.py tests/tools/test_cronjob_unattended_ack.py
git commit -m "feat(cron): create-time acknowledgment gate for approval-gated tools"
```

---

### Task 9: Grant Stripe toolset to the operator role

**Files:**
- Modify: `gateway/tool_access.py:48`
- Test: `tests/gateway/test_tool_access.py` (extend)

**Interfaces:**
- Produces: `operator` and `admin` grant the `stripe` (canonical `mcp-stripe`) toolset; `readonly`/`chat_only` do not.

> Note the alias: MCP registers the toolset as `mcp-stripe` with a bare `stripe` alias. Grant `stripe` (the alias form used in `enabled_toolsets` and the GitHub precedent). Confirm during implementation that the enforcement resolves `mcp-stripe` against a `stripe` grant; if it keys on the canonical name, grant `"mcp-stripe"` instead (add BOTH if unsure — `_granted` treats them as independent exact matches).

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_tool_access.py  (add)
from gateway.tool_access import BUILTIN_ROLES, _granted


def test_operator_and_admin_grant_stripe():
    assert _granted(BUILTIN_ROLES["operator"], "stripe")
    assert _granted(BUILTIN_ROLES["admin"], "stripe")


def test_restricted_roles_do_not_grant_stripe():
    assert not _granted(BUILTIN_ROLES["readonly"], "stripe")
    assert not _granted(BUILTIN_ROLES["chat_only"], "stripe")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py -k stripe`
Expected: FAIL — `operator` does not yet grant `stripe`.

- [ ] **Step 3: Add `stripe` to the operator frozenset**

In `gateway/tool_access.py:48`:

```python
    "operator": frozenset(
        {"file", "web", "browser", "vision", "memory", "delegation",
         "notion", "session_search", "stripe"}
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py -k stripe`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gateway/tool_access.py tests/gateway/test_tool_access.py
git commit -m "feat(rbac): grant Stripe toolset to the operator role"
```

---

### Task 10: Stripe MCP wiring + memory note + verification (ops/docs)

**Files:**
- No repo code. VM `~/.hermes/config.yaml` + `~/.hermes/.env` (operator applies).
- Create: a memory note file documenting the recipe.

> This task produces config the user applies on the deployed VM (the agent cannot reach it) plus a durable memory note. No automated test — verification is manual via `hermes mcp` commands.

- [ ] **Step 1: Provide the config recipe (hand to the operator)**

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  stripe:
    url: "https://mcp.stripe.com"
    headers:
      Authorization: "Bearer ${STRIPE_API_KEY}"   # interpolated from ~/.hermes/.env
    timeout: 120
    connect_timeout: 60
    enabled: true

approvals:
  require_for_tools:
    - stripe_api_write
    - create_refund
```

`~/.hermes/.env`: `STRIPE_API_KEY=rk_live_...` — a **least-privilege restricted key** (write only on the resources actually needed; no Connect / payout / key-management scopes).

- [ ] **Step 2: Verify the MCP connection (operator runs on the VM)**

Run: `hermes mcp list` then `hermes mcp test stripe`
Expected: `stripe` connects with no OAuth prompt and lists the Stripe tools (`stripe_api_read`, `stripe_api_write`, `create_refund`, `get_stripe_account_info`, …). Registers under toolset `mcp-stripe`.

- [ ] **Step 3: Verify the gate fires**

In a Slack/CLI session as an operator, ask the agent to read account info (`get_stripe_account_info`) — succeeds silently. Then ask it to perform a write (e.g. create a test customer via `stripe_api_write`) — expect the approval prompt (Allow once / Allow session); deny and confirm it does not execute.

- [ ] **Step 4: Write the memory note**

Create a memory file (mirror the existing `github-remote-mcp-hermes` note) capturing: the `mcp_servers.stripe` block, no-`auth`→no-OAuth, no read-only header (key is the boundary), `require_for_tools` gating `stripe_api_write`/`create_refund`, operator+admin RBAC, and the cron `unattended_approved_tools` requirement. Add its one-line pointer to `MEMORY.md`.

- [ ] **Step 5: Full-suite regression + commit the memory note**

Run: `scripts/run_tests.sh`
Expected: PASS

```bash
git add <memory-note-path> MEMORY.md
git commit -m "docs(memory): Stripe hosted MCP recipe + per-tool approval gate"
```

---

## Self-Review

**Spec coverage:**
- Config schema (`require_for_tools`) → Task 1. ✓
- Enforcement at `pre_tool_call` beside RBAC backstop → Task 5. ✓
- Interactive once/session, `allow_permanent=False` → Task 4. ✓
- Redacted args → Task 2. ✓
- Gateway blocking (Slack) → Task 3 + Task 4. ✓
- Cron skip-when-authorized (standing permission AND ack) → Tasks 6–7. ✓
- Create-time ack (`unattended_approved_tools`) → Task 8. ✓
- Audit of unattended runs → Task 6 (`_audit_cron_tool` → `record_access`). ✓
- Stripe RBAC operator grant → Task 9. ✓
- Stripe MCP wiring + memory note + verification → Task 10. ✓
- Backward-compat / orthogonal to `mode` → Global Constraints + Task 1 inert test. ✓
- RBAC precedence → Task 5 insertion point (after backstop). ✓

**Placeholder scan:** `_cron_tool_decision` is introduced as an explicit, working stub in Task 4 (fails closed via `cron_mode`) and REPLACED with the full implementation in Task 6 — this is a deliberate staged build, not a placeholder; both versions are complete code.

**Type consistency:** `check_tool_approval(tool_name, args, session_key) -> dict` with `{"approved": bool, "message": Optional[str]}` is used identically in Tasks 4, 5, 6. `pattern_key = f"tool:{tool_name}"` is consistent across Tasks 3, 4, 6. `set_cron_tool_context(owner_grant=, acked_tools=)` / `get_cron_tool_context() -> (grant, acked_set)` match between Tasks 6 and 7. `_toolset_for_tool` and `_registry_tools_for_toolset` are the only tool→toolset resolvers, used in Tasks 6 and 8 respectively.

**Open confirmation (carry into execution):** Task 3 assumes `_ApprovalEntry` exposes `.event` (threading.Event) and `.result` (str) — confirm at implementation from `tools/approval.py:580`. Task 9 assumes the RBAC enforcement resolves `mcp-stripe` against a `stripe` grant — confirm and grant `mcp-stripe` too if not.
