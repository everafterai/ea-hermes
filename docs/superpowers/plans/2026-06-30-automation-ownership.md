# Automation Ownership & Cross-User Edit Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record who owns each user-built automation (skill, cron job, script, automation bundle) and, when an identified user tries to edit one owned by someone else, enforce a code-level confirmation gate at the tool chokepoint, notify the owner on a confirmed edit, and nudge users to claim unowned legacy items.

**Architecture:** One pure module (`agent/automation_ownership.py`) owns a JSON registry (`~/.hermes/ownership/registry.json`), the artifact-key scheme, the `check_edit` decision logic, ownership mutations, and owner notification. Thin hooks at the existing edit chokepoints — `skill_manage`, `cronjob`, and the `write_file`/`patch` file tools — call `check_edit` before mutating and `register_creator` on create, each gaining a `confirm_cross_user_owner` parameter (modeled on the existing `cross_profile` soft-guard opt-out). A short `AUTOMATION_OWNERSHIP_GUIDANCE` block is appended to the cached `stable` system-prompt segment so the agent always knows the rules. A `hermes own` CLI manages ownership and scaffolds optional `~/.hermes/automations/<name>/` bundles. Soft awareness layer, not a security boundary.

**Tech Stack:** Python 3.11, pytest via `scripts/run_tests.sh`, `pathlib`, `json`, `dataclasses`. Config read via `hermes_cli.config.read_raw_config`; identity via `gateway.session_context.get_session_env`; audit via `agent.data_access_audit.record_access`; DM via `tools.send_message_tool.send_message_tool`.

## Global Constraints

- **Run tests only via `scripts/run_tests.sh`** (CI-parity: unset creds, `TZ=UTC`, `C.UTF-8`, xdist, per-test subprocess isolation). Never bare `pytest`.
- **Never write to the real `~/.hermes/`** — the autouse `_hermetic_environment` fixture (tests/conftest.py) redirects `HERMES_HOME` to a per-test tempdir and pre-creates `sessions/ cron/ memories/ skills/`. Tests rely on that redirect; do not hardcode `~/.hermes`.
- **Use `get_hermes_home()` from `hermes_constants`** — never hardcode `~/.hermes`.
- **No change-detector tests** — assert behavior, not implementation strings.
- **Fail-open, never raise into the tool path:** a broken/missing/corrupt registry or config must degrade to "no ownership data" (everything unowned, no gate) and must never raise out of a hook. The *only* blocking outcomes are the intentional `CROSS_USER`-unacknowledged and `NO_IDENTITY`-on-owned refusals.
- **Not a security boundary:** user-facing gate/denial messages stay honest — this is an awareness + collaboration layer; a determined actor with shell/RBAC access is out of scope. RBAC (`gateway/tool_access.py`) remains the real boundary.
- **Config read pattern:** mirror `agent/data_access_audit.py` exactly — read the top-level `automation_ownership:` block via `read_raw_config()`. **No `gateway/config.py` change is needed** (it surfaces arbitrary top-level keys already).
- **`ruff` is near-disabled** (only PLW1514 enforced — always pass `encoding=` to `open()`). Run `ruff check .` and `ty check` before the final commit.
- **Every commit ends with the trailer:**
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

---

### Task 1: Registry data layer — config, identity, keys, path classification (`agent/automation_ownership.py`)

**Files:**
- Create: `agent/automation_ownership.py`
- Test: `tests/agent/test_automation_ownership_registry.py` (create)

**Interfaces:**
- Produces (consumed by all later tasks):
  - `@dataclass(frozen=True) class Identity: platform: str; user_id: str; display_name: str`
  - `current_identity() -> Optional[Identity]` — from session contextvars; `None` when `user_id` is empty.
  - `is_enabled() -> bool` (config `automation_ownership.enabled`, default `True`); `notify_enabled() -> bool` (`automation_ownership.notify_owner`, default `True`).
  - `artifact_key(kind: str, stable_id: str) -> str` → `"<kind>:<stable_id>"`.
  - `path_to_artifact_key(path) -> Optional[tuple[str, str]]` → `(key, kind)` for a path under `<HERMES_HOME>/scripts|skills|automations/...`, else `None`.
  - `get_record(key: str) -> Optional[dict]`; `_load_registry() -> dict`; `_save_registry(reg: dict) -> None`; `_put_record(key, record) -> None`; `_delete_record(key) -> None`.
- Consumes: `hermes_constants.get_hermes_home`, `hermes_cli.config.read_raw_config`, `gateway.session_context.get_session_env`.

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_automation_ownership_registry.py`:

```python
"""Registry data layer: config, identity, key scheme, path classification."""
import json

import agent.automation_ownership as ao
from hermes_constants import get_hermes_home


def _touch(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_artifact_key_format():
    assert ao.artifact_key("skill", "weekly-report") == "skill:weekly-report"
    assert ao.artifact_key("cron", "9f3a1c2b7e10") == "cron:9f3a1c2b7e10"


def test_path_to_artifact_key_script():
    p = get_hermes_home() / "scripts" / "reports" / "weekly.py"
    assert ao.path_to_artifact_key(str(p)) == ("script:reports/weekly.py", "script")


def test_path_to_artifact_key_skill_file():
    p = get_hermes_home() / "skills" / "weekly-report" / "SKILL.md"
    assert ao.path_to_artifact_key(str(p)) == ("skill:weekly-report", "skill")


def test_path_to_artifact_key_categorized_skill():
    p = get_hermes_home() / "skills" / "mlops" / "weekly-report" / "references" / "x.md"
    # The skill NAME is the leaf dir that directly contains SKILL.md; here we
    # only have the path, so classification keys on the dir holding SKILL.md.
    _touch(get_hermes_home() / "skills" / "mlops" / "weekly-report" / "SKILL.md")
    assert ao.path_to_artifact_key(str(p)) == ("skill:weekly-report", "skill")


def test_path_to_artifact_key_automation_bundle():
    p = get_hermes_home() / "automations" / "weekly-report" / "scripts" / "run.sh"
    assert ao.path_to_artifact_key(str(p)) == ("automation:weekly-report", "automation")


def test_path_to_artifact_key_outside_returns_none(tmp_path):
    assert ao.path_to_artifact_key(str(tmp_path / "scripts" / "x.sh")) is None
    # A file directly under HERMES_HOME but not in a managed dir:
    assert ao.path_to_artifact_key(str(get_hermes_home() / "config.yaml")) is None


def test_registry_round_trip():
    rec = {"kind": "cron", "owner": {"platform": "slack", "user_id": "U1", "display_name": "Alice"},
           "collaborators": [], "source": "creator"}
    ao._put_record("cron:abc", rec)
    assert ao.get_record("cron:abc")["owner"]["user_id"] == "U1"
    # Persisted to disk as JSON under ownership/registry.json
    reg = json.loads((get_hermes_home() / "ownership" / "registry.json").read_text(encoding="utf-8"))
    assert reg["automations"]["cron:abc"]["owner"]["user_id"] == "U1"


def test_corrupt_registry_degrades_to_empty():
    path = get_hermes_home() / "ownership" / "registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert ao._load_registry() == {"version": 1, "automations": {}}
    assert ao.get_record("anything") is None  # must not raise


def test_is_enabled_default_true(monkeypatch):
    monkeypatch.setattr(ao, "_config", lambda: {})
    assert ao.is_enabled() is True
    monkeypatch.setattr(ao, "_config", lambda: {"enabled": False})
    assert ao.is_enabled() is False


def test_current_identity_none_without_user(monkeypatch):
    monkeypatch.setattr(ao, "get_session_env", lambda name, default="": "")
    assert ao.current_identity() is None


def test_current_identity_from_session(monkeypatch):
    vals = {"HERMES_SESSION_USER_ID": "U7", "HERMES_SESSION_USER_NAME": "Bob",
            "HERMES_SESSION_PLATFORM": "slack"}
    monkeypatch.setattr(ao, "get_session_env", lambda name, default="": vals.get(name, default))
    ident = ao.current_identity()
    assert ident.user_id == "U7" and ident.display_name == "Bob" and ident.platform == "slack"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_registry.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.automation_ownership'`.

- [ ] **Step 3: Create the module (data layer)**

Create `agent/automation_ownership.py`:

```python
"""Ownership registry for user-built automations (skills, crons, scripts, bundles).

Records which identified platform user owns each automation, so the tool layer can
surface a confirmation gate when someone edits another user's work, notify the owner,
and nudge users to claim unowned legacy items.

NOT a security boundary: the registry is a local JSON file written by the same OS uid
that runs the gateway, and RBAC (gateway/tool_access.py) remains the real tool-access
boundary. This is an awareness + collaboration layer. Every function fails open and
never raises into the tool path; the only intentional "block" is the cross-user gate,
which the callers translate into a tool error string.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from gateway.session_context import get_session_env

_MANAGED_DIRS = ("scripts", "skills", "automations")


# --------------------------------------------------------------------------- #
# Config (mirrors agent/data_access_audit.py: read the top-level block via
# read_raw_config; no gateway/config.py change required).
# --------------------------------------------------------------------------- #
def _config() -> dict:
    """Return the ``automation_ownership`` config block, or {} on any failure."""
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config().get("automation_ownership", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def is_enabled() -> bool:
    try:
        return bool(_config().get("enabled", True))
    except Exception:
        return True


def notify_enabled() -> bool:
    try:
        return bool(_config().get("notify_owner", True))
    except Exception:
        return True


def _registry_path() -> Path:
    try:
        raw = _config().get("registry_path") or ""
    except Exception:
        raw = ""
    if raw:
        try:
            return Path(os.path.expanduser(str(raw)))
        except Exception:
            pass
    return get_hermes_home() / "ownership" / "registry.json"


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Identity:
    platform: str
    user_id: str
    display_name: str


def current_identity() -> Optional[Identity]:
    """Acting user's identity from session contextvars, or None when no human."""
    try:
        user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        if not user_id:
            return None
        return Identity(
            platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
            user_id=user_id,
            display_name=get_session_env("HERMES_SESSION_USER_NAME", "") or user_id,
        )
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Registry I/O (atomic temp + replace, 0o700 dir / 0o600 file like cron storage)
# --------------------------------------------------------------------------- #
def _empty_registry() -> dict:
    return {"version": 1, "automations": {}}


def _load_registry() -> dict:
    try:
        path = _registry_path()
        if not path.exists():
            return _empty_registry()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("automations"), dict):
            return _empty_registry()
        return data
    except Exception:
        # Corrupt/unreadable registry must not break ownership reads.
        return _empty_registry()


def _save_registry(reg: dict) -> None:
    try:
        path = _registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except Exception:
            pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception:
        # Best-effort persistence; a write failure must not raise into a tool.
        pass


def get_record(key: str) -> Optional[dict]:
    rec = _load_registry()["automations"].get(key)
    return rec if isinstance(rec, dict) else None


def _put_record(key: str, record: dict) -> None:
    reg = _load_registry()
    reg["automations"][key] = record
    _save_registry(reg)


def _delete_record(key: str) -> None:
    reg = _load_registry()
    if key in reg["automations"]:
        del reg["automations"][key]
        _save_registry(reg)


# --------------------------------------------------------------------------- #
# Artifact keys + path classification
# --------------------------------------------------------------------------- #
def artifact_key(kind: str, stable_id: str) -> str:
    return f"{kind}:{stable_id}"


def _skill_name_for_path(resolved: Path, skills_root: Path) -> Optional[str]:
    """Walk up from *resolved* to the directory that directly contains SKILL.md,
    under skills_root; return that dir's name (the skill name)."""
    cur = resolved if resolved.is_dir() else resolved.parent
    while True:
        try:
            cur.relative_to(skills_root)
        except ValueError:
            return None
        if cur == skills_root:
            return None
        if (cur / "SKILL.md").exists() or cur.parent == skills_root:
            # leaf skill dir is the one whose parent is skills_root OR holds SKILL.md
            return cur.name
        cur = cur.parent


def path_to_artifact_key(path) -> Optional[Tuple[str, str]]:
    """Return (key, kind) if *path* is a managed automation file, else None.

    Anchored to the active HERMES_HOME:
      * scripts/<relpath>            -> ("script:<relpath>", "script")
      * skills/[cat/]<name>/...      -> ("skill:<name>", "skill")
      * automations/<name>/...       -> ("automation:<name>", "automation")
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        return None
    home = get_hermes_home().resolve()

    scripts_root = (home / "scripts")
    try:
        rel = resolved.relative_to(scripts_root)
        return artifact_key("script", rel.as_posix()), "script"
    except ValueError:
        pass

    skills_root = (home / "skills")
    name = _skill_name_for_path(resolved, skills_root)
    if name:
        return artifact_key("skill", name), "skill"

    autos_root = (home / "automations")
    try:
        rel = resolved.relative_to(autos_root)
        bundle = rel.parts[0] if rel.parts else ""
        if bundle:
            return artifact_key("automation", bundle), "automation"
    except ValueError:
        pass

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_registry.py`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/automation_ownership.py tests/agent/test_automation_ownership_registry.py
git commit -m "feat(automations): registry data layer for automation ownership

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Decision logic + ownership mutations (`agent/automation_ownership.py`)

**Files:**
- Modify: `agent/automation_ownership.py` (append decision + mutation API after Task 1 code)
- Test: `tests/agent/test_automation_ownership_decision.py` (create)

**Interfaces:**
- Consumes: everything from Task 1.
- Produces (consumed by hook Tasks 4–6 and CLI Task 8):
  - `class EditDecision(Enum)`: `OWNER`, `COLLABORATOR`, `UNOWNED`, `CROSS_USER`, `NO_IDENTITY`.
  - `@dataclass class EditResult: decision: EditDecision; allowed: bool; message: str; record: Optional[dict]`.
  - `check_edit(key: str, identity: Optional[Identity], *, confirm: Optional[str] = None) -> EditResult`.
  - `register_creator(key: str, kind: str, identity: Optional[Identity]) -> None` — sets owner=creator iff no record exists; no-op otherwise.
  - `claim(key: str, kind: str, identity: Identity) -> dict`.
  - `transfer(key: str, new_owner: Identity, *, by: Identity, by_is_admin: bool = False) -> dict`.
  - `add_collaborator(key: str, ident: Identity) -> dict`; `remove_collaborator(key: str, user_id: str) -> dict`.
  - `list_for_user(user_id: str) -> dict` → `{"owned": [...keys], "collaborator": [...keys]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_automation_ownership_decision.py`:

```python
"""check_edit truth table + ownership mutations."""
import agent.automation_ownership as ao
from agent.automation_ownership import Identity, EditDecision

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _own(key="cron:j1", owner=ALICE, collaborators=()):
    ao._put_record(key, {
        "kind": "cron",
        "owner": {"platform": owner.platform, "user_id": owner.user_id, "display_name": owner.display_name},
        "collaborators": [{"platform": c.platform, "user_id": c.user_id, "display_name": c.display_name}
                          for c in collaborators],
        "source": "creator",
    })
    return key


def test_owner_allowed_silent():
    k = _own()
    r = ao.check_edit(k, ALICE)
    assert r.decision == EditDecision.OWNER and r.allowed and r.message == ""


def test_collaborator_allowed_silent():
    k = _own(collaborators=(BOB,))
    r = ao.check_edit(k, BOB)
    assert r.decision == EditDecision.COLLABORATOR and r.allowed


def test_cross_user_blocked_without_confirm():
    k = _own()
    r = ao.check_edit(k, BOB)
    assert r.decision == EditDecision.CROSS_USER and not r.allowed
    assert "Alice" in r.message and "confirm_cross_user_owner" in r.message


def test_cross_user_allowed_with_matching_display_name():
    k = _own()
    r = ao.check_edit(k, BOB, confirm="Alice")
    assert r.decision == EditDecision.CROSS_USER and r.allowed


def test_cross_user_allowed_with_matching_user_id():
    k = _own()
    r = ao.check_edit(k, BOB, confirm="U_ALICE")
    assert r.allowed


def test_cross_user_wrong_confirm_still_blocked():
    k = _own()
    r = ao.check_edit(k, BOB, confirm="Carol")
    assert not r.allowed


def test_unowned_allowed_with_claim_nudge():
    r = ao.check_edit("cron:unknown", BOB)
    assert r.decision == EditDecision.UNOWNED and r.allowed
    assert "claim" in r.message.lower()


def test_unowned_no_identity_allowed_silent():
    r = ao.check_edit("cron:unknown", None)
    assert r.allowed and r.message == ""


def test_owned_no_identity_blocked():
    k = _own()
    r = ao.check_edit(k, None)
    assert r.decision == EditDecision.NO_IDENTITY and not r.allowed


def test_register_creator_sets_owner_once():
    ao.register_creator("skill:s1", "skill", ALICE)
    assert ao.get_record("skill:s1")["owner"]["user_id"] == "U_ALICE"
    # second call by Bob must NOT overwrite an existing owner
    ao.register_creator("skill:s1", "skill", BOB)
    assert ao.get_record("skill:s1")["owner"]["user_id"] == "U_ALICE"


def test_register_creator_noop_without_identity():
    ao.register_creator("skill:s2", "skill", None)
    assert ao.get_record("skill:s2") is None


def test_claim_assigns_owner():
    rec = ao.claim("script:foo.sh", "script", BOB)
    assert rec["owner"]["user_id"] == "U_BOB" and rec["source"] == "claim"


def test_transfer_by_owner():
    k = _own()
    rec = ao.transfer(k, BOB, by=ALICE)
    assert rec["owner"]["user_id"] == "U_BOB"


def test_transfer_by_non_owner_non_admin_raises():
    k = _own()
    try:
        ao.transfer(k, BOB, by=BOB)
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_transfer_by_admin_allowed():
    k = _own()
    rec = ao.transfer(k, BOB, by=Identity("slack", "U_ADMIN", "Admin"), by_is_admin=True)
    assert rec["owner"]["user_id"] == "U_BOB"


def test_add_and_remove_collaborator():
    k = _own()
    ao.add_collaborator(k, BOB)
    assert any(c["user_id"] == "U_BOB" for c in ao.get_record(k)["collaborators"])
    ao.remove_collaborator(k, "U_BOB")
    assert all(c["user_id"] != "U_BOB" for c in ao.get_record(k)["collaborators"])


def test_list_for_user():
    ao._put_record("cron:a", {"kind": "cron", "owner": {"platform": "slack", "user_id": "U_ALICE", "display_name": "Alice"}, "collaborators": []})
    ao._put_record("cron:b", {"kind": "cron", "owner": {"platform": "slack", "user_id": "U_BOB", "display_name": "Bob"}, "collaborators": [{"platform": "slack", "user_id": "U_ALICE", "display_name": "Alice"}]})
    out = ao.list_for_user("U_ALICE")
    assert "cron:a" in out["owned"] and "cron:b" in out["collaborator"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_decision.py`
Expected: FAIL — `ImportError: cannot import name 'EditDecision'`.

- [ ] **Step 3: Append the decision + mutation API**

Append to `agent/automation_ownership.py`:

```python
# --------------------------------------------------------------------------- #
# Decision logic
# --------------------------------------------------------------------------- #
class EditDecision(Enum):
    OWNER = "owner"
    COLLABORATOR = "collaborator"
    UNOWNED = "unowned"
    CROSS_USER = "cross_user"
    NO_IDENTITY = "no_identity"


@dataclass
class EditResult:
    decision: EditDecision
    allowed: bool
    message: str = ""
    record: Optional[dict] = None


def _ident_dict(ident: Identity) -> dict:
    return {"platform": ident.platform, "user_id": ident.user_id, "display_name": ident.display_name}


def _ack_matches(record: dict, confirm: str) -> bool:
    owner = record.get("owner") or {}
    c = (confirm or "").strip().lower()
    if not c:
        return False
    return c == (owner.get("user_id") or "").lower() or c == (owner.get("display_name") or "").lower()


def _cross_user_message(record: dict) -> str:
    owner = record.get("owner") or {}
    who = owner.get("display_name") or owner.get("user_id") or "another user"
    return (
        f"This automation is owned by {who}. You are not an owner or collaborator. "
        f"Confirm with the user first, then re-invoke the SAME call with "
        f'confirm_cross_user_owner="{who}" to proceed (the owner will be notified). '
        "Do not confirm on the user's behalf. (Ownership is an awareness layer, "
        "not a hard permission — but cross-user edits are gated and logged.)"
    )


def _claim_nudge(key: str) -> str:
    return (
        f"This automation ({key}) has no recorded owner. If it's yours, offer to "
        f'claim it: `hermes own claim {key}`. Proceeding with the edit.'
    )


def check_edit(key: str, identity: Optional[Identity], *, confirm: Optional[str] = None) -> EditResult:
    """Decide whether *identity* may edit the automation at *key*. Pure; no I/O side effects."""
    try:
        record = get_record(key)
    except Exception:
        record = None

    if record is None:
        if identity is None:
            return EditResult(EditDecision.UNOWNED, True, "", None)
        return EditResult(EditDecision.UNOWNED, True, _claim_nudge(key), None)

    if identity is None:
        return EditResult(
            EditDecision.NO_IDENTITY, False,
            f"Refusing autonomous edit of an owned automation ({key}); no acting user "
            "identity to confirm the change. Run this interactively to proceed.",
            record,
        )

    owner = record.get("owner") or {}
    if identity.user_id and identity.user_id == owner.get("user_id"):
        return EditResult(EditDecision.OWNER, True, "", record)
    if any(identity.user_id == (c or {}).get("user_id") for c in record.get("collaborators", [])):
        return EditResult(EditDecision.COLLABORATOR, True, "", record)

    if confirm and _ack_matches(record, confirm):
        return EditResult(EditDecision.CROSS_USER, True, "", record)
    return EditResult(EditDecision.CROSS_USER, False, _cross_user_message(record), record)


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def register_creator(key: str, kind: str, identity: Optional[Identity]) -> None:
    """Record *identity* as owner of a freshly created automation. No-op if the
    automation already has a record, or there is no acting identity."""
    try:
        if identity is None or get_record(key) is not None:
            return
        _put_record(key, {
            "kind": kind,
            "owner": _ident_dict(identity),
            "collaborators": [],
            "source": "creator",
        })
    except Exception:
        pass


def claim(key: str, kind: str, identity: Identity) -> dict:
    rec = get_record(key) or {"kind": kind, "collaborators": []}
    rec["kind"] = rec.get("kind", kind)
    rec["owner"] = _ident_dict(identity)
    rec.setdefault("collaborators", [])
    rec["source"] = "claim"
    _put_record(key, rec)
    return rec


def transfer(key: str, new_owner: Identity, *, by: Identity, by_is_admin: bool = False) -> dict:
    rec = get_record(key)
    if rec is None:
        raise KeyError(f"No ownership record for {key}")
    owner = rec.get("owner") or {}
    if not by_is_admin and by.user_id != owner.get("user_id"):
        raise PermissionError("Only the current owner or an admin may transfer ownership.")
    rec["owner"] = _ident_dict(new_owner)
    rec["source"] = "transfer"
    _put_record(key, rec)
    return rec


def add_collaborator(key: str, ident: Identity) -> dict:
    rec = get_record(key)
    if rec is None:
        raise KeyError(f"No ownership record for {key}")
    rec.setdefault("collaborators", [])
    if not any(c.get("user_id") == ident.user_id for c in rec["collaborators"]):
        rec["collaborators"].append(_ident_dict(ident))
        _put_record(key, rec)
    return rec


def remove_collaborator(key: str, user_id: str) -> dict:
    rec = get_record(key)
    if rec is None:
        raise KeyError(f"No ownership record for {key}")
    rec["collaborators"] = [c for c in rec.get("collaborators", []) if c.get("user_id") != user_id]
    _put_record(key, rec)
    return rec


def list_for_user(user_id: str) -> dict:
    owned: List[str] = []
    collab: List[str] = []
    for key, rec in _load_registry()["automations"].items():
        if (rec.get("owner") or {}).get("user_id") == user_id:
            owned.append(key)
        elif any(c.get("user_id") == user_id for c in rec.get("collaborators", [])):
            collab.append(key)
    return {"owned": owned, "collaborator": collab}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_decision.py`
Expected: PASS (17 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/automation_ownership.py tests/agent/test_automation_ownership_decision.py
git commit -m "feat(automations): check_edit decision logic + ownership mutations

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Owner notification + audit (`agent/automation_ownership.py`)

**Files:**
- Modify: `agent/automation_ownership.py` (append `record_and_notify`)
- Test: `tests/agent/test_automation_ownership_notify.py` (create)

**Interfaces:**
- Consumes: `agent.data_access_audit.record_access`, `tools.send_message_tool.send_message_tool`, Task 2 records.
- Produces: `record_and_notify(key: str, editor: Identity, record: dict) -> None` — append an `automation_edit` audit event and (if `notify_owner` and the owner has a reachable DM) DM the owner. Best-effort; never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_automation_ownership_notify.py`:

```python
"""record_and_notify: audit event + best-effort owner DM."""
import json

import agent.automation_ownership as ao
import agent.data_access_audit as audit
from agent.automation_ownership import Identity
from hermes_constants import get_hermes_home

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _audit_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _record():
    return {"kind": "cron", "owner": {"platform": "slack", "user_id": "U_ALICE", "display_name": "Alice"},
            "collaborators": []}


def test_notify_writes_audit_event(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "notify_enabled", lambda: True)
    sent = {}
    monkeypatch.setattr(ao, "_send_dm", lambda platform, user_id, msg: sent.update(
        {"platform": platform, "user_id": user_id, "msg": msg}) or True)

    ao.record_and_notify("cron:j1", BOB, _record())

    lines = _audit_lines()
    assert len(lines) == 1
    assert lines[0]["action"] == "automation_edit"
    assert lines[0]["target"] == "cron:j1"
    assert sent["user_id"] == "U_ALICE" and "Bob" in sent["msg"]


def test_notify_disabled_skips_dm(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "notify_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: calls.append(a) or True)
    ao.record_and_notify("cron:j1", BOB, _record())
    assert calls == []                       # no DM
    assert len(_audit_lines()) == 1          # audit still written


def test_notify_never_raises_on_dm_failure(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "notify_enabled", lambda: True)
    def boom(*a, **k):
        raise RuntimeError("slack down")
    monkeypatch.setattr(ao, "_send_dm", boom)
    ao.record_and_notify("cron:j1", BOB, _record())   # must not raise
    assert len(_audit_lines()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_notify.py`
Expected: FAIL — `AttributeError: module 'agent.automation_ownership' has no attribute 'record_and_notify'`.

- [ ] **Step 3: Append notification + audit**

Append to `agent/automation_ownership.py`:

```python
# --------------------------------------------------------------------------- #
# Notification + audit
# --------------------------------------------------------------------------- #
def _send_dm(platform: str, user_id: str, message: str) -> bool:
    """Best-effort DM to a platform user. Returns True on apparent success.

    Reuses send_message_tool, which resolves a Slack U-id to a DM channel via
    conversations.open. Isolated in its own function so tests can stub it.
    """
    try:
        from tools.send_message_tool import send_message_tool

        out = send_message_tool({
            "action": "send",
            "target": f"{platform}:{user_id}",
            "message": message,
        })
        try:
            return "error" not in json.loads(out)
        except Exception:
            return True
    except Exception:
        return False


def record_and_notify(key: str, editor: Identity, record: dict) -> None:
    """Audit a confirmed cross-user edit and DM the owner. Best-effort; never raises."""
    try:
        from agent.data_access_audit import record_access

        record_access(
            tool="automation_ownership",
            action="automation_edit",
            target=f"{key} edited by {editor.user_id}",
        )
    except Exception:
        pass

    try:
        if not notify_enabled():
            return
        owner = record.get("owner") or {}
        platform = owner.get("platform") or editor.platform
        owner_id = owner.get("user_id") or ""
        if not owner_id:
            return
        editor_name = editor.display_name or editor.user_id
        msg = (
            f":warning: {editor_name} edited your automation `{key}`. "
            "— via Hermes automation ownership"
        )
        _send_dm(platform, owner_id, msg)
    except Exception:
        # Notification is best-effort; never reverse or block the edit.
        pass
```

Note: the `target` passed to `record_access` includes the editor id; `record_access` itself also stamps the session identity fields, so the editor is captured both ways.

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_notify.py`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/automation_ownership.py tests/agent/test_automation_ownership_notify.py
git commit -m "feat(automations): owner DM + audit on confirmed cross-user edit

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Gate the skill tool (`tools/skill_manager_tool.py`)

**Files:**
- Modify: `tools/skill_manager_tool.py` (dispatch `skill_manage` line 825; handlers `_create_skill` 485, `_edit_skill` 542, `_patch_skill` 575, `_delete_skill` 669, `_write_file` 726, `_remove_file` 778; schema 909; registry handler lambda 1031)
- Test: `tests/tools/test_skill_ownership.py` (create)

**Interfaces:**
- Consumes: `agent.automation_ownership` (`current_identity`, `artifact_key`, `check_edit`, `register_creator`, `record_and_notify`, `is_enabled`).
- Produces: `skill_manage(..., confirm_cross_user_owner: str = None)` gates cross-user edits and registers the creator on `create`.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_skill_ownership.py`:

```python
"""skill_manage enforces the cross-user ownership gate and registers creators."""
import json

import agent.automation_ownership as ao
import agent.data_access_audit as audit
import tools.skill_manager_tool as smt
from agent.automation_ownership import Identity

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")

_SKILL = """---
name: weekly-report
description: A weekly report skill for testing ownership gating behavior here.
---
Body.
"""

_SKILL_V2 = _SKILL.replace("Body.", "Body v2.")


def _as(identity, monkeypatch):
    monkeypatch.setattr(ao, "current_identity", lambda: identity)
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: True)


def test_create_registers_owner(monkeypatch):
    _as(ALICE, monkeypatch)
    out = json.loads(smt.skill_manage(action="create", name="weekly-report", content=_SKILL))
    assert "error" not in out
    assert ao.get_record("skill:weekly-report")["owner"]["user_id"] == "U_ALICE"


def test_cross_user_edit_blocked_without_confirm(monkeypatch):
    _as(ALICE, monkeypatch)
    smt.skill_manage(action="create", name="weekly-report", content=_SKILL)
    _as(BOB, monkeypatch)
    out = json.loads(smt.skill_manage(action="edit", name="weekly-report", content=_SKILL_V2))
    assert "error" in out
    assert "owned by Alice" in out["error"]


def test_cross_user_edit_allowed_with_confirm(monkeypatch):
    _as(ALICE, monkeypatch)
    smt.skill_manage(action="create", name="weekly-report", content=_SKILL)
    _as(BOB, monkeypatch)
    out = json.loads(smt.skill_manage(
        action="edit", name="weekly-report", content=_SKILL_V2,
        confirm_cross_user_owner="Alice"))
    assert "error" not in out


def test_owner_edits_freely(monkeypatch):
    _as(ALICE, monkeypatch)
    smt.skill_manage(action="create", name="weekly-report", content=_SKILL)
    out = json.loads(smt.skill_manage(action="edit", name="weekly-report", content=_SKILL_V2))
    assert "error" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_skill_ownership.py`
Expected: FAIL — `test_cross_user_edit_blocked_without_confirm` fails (Bob's edit currently succeeds) and `skill_manage` rejects the unexpected `confirm_cross_user_owner` kwarg.

- [ ] **Step 3a: Add a shared gate helper near the top of the handlers**

In `tools/skill_manager_tool.py`, add this helper just above `def _create_skill(` (line 485):

```python
def _ownership_gate(key: str):
    """Return (error_str, pending_notify) for an edit of *key* by the current user.

    error_str is non-None when the edit must be refused. pending_notify is a
    (key, editor, record) tuple to fire after a successful confirmed cross-user
    edit, else None. Fails open: any internal error -> allow.
    """
    try:
        from agent import automation_ownership as _ao
        if not _ao.is_enabled():
            return None, None
        ident = _ao.current_identity()
        res = _ao.check_edit(key, ident, confirm=_CONFIRM_OWNER.get())
        if not res.allowed:
            return res.message, None
        if res.decision == _ao.EditDecision.CROSS_USER and res.record is not None and ident is not None:
            return None, (key, ident, res.record)
        return None, None
    except Exception:
        return None, None
```

And add a module-level contextvar to carry the confirm token without threading it through every handler signature. Near the top of the file (after imports), add:

```python
from contextvars import ContextVar
_CONFIRM_OWNER: ContextVar = ContextVar("_skill_confirm_owner", default=None)
```

- [ ] **Step 3b: Set the confirm token + register creator in `skill_manage`**

In `skill_manage` (line 825), wrap the dispatch so the confirm token is set for the duration of the call, and register the creator after a successful create. Replace the action-routing block (lines ~842–872) body with a version that (1) sets `_CONFIRM_OWNER`, (2) calls the handler, (3) on a successful `create` registers the creator. Concretely, at the very start of `skill_manage`'s body add:

```python
    _tok = _CONFIRM_OWNER.set(confirm_cross_user_owner)
    try:
        result = _skill_manage_inner(
            action=action, name=name, content=content, category=category,
            file_path=file_path, file_content=file_content, old_string=old_string,
            new_string=new_string, replace_all=replace_all, absorbed_into=absorbed_into,
        )
        try:
            import json as _json
            from agent import automation_ownership as _ao
            if action == "create" and "error" not in _json.loads(result):
                _ao.register_creator(_ao.artifact_key("skill", name), "skill",
                                     _ao.current_identity())
        except Exception:
            pass
        return result
    finally:
        _CONFIRM_OWNER.reset(_tok)
```

Rename the existing body of `skill_manage` (the current action-routing function) to `_skill_manage_inner(...)` with the same parameters **except** `confirm_cross_user_owner`, and add `confirm_cross_user_owner: str = None` to the public `skill_manage` signature.

- [ ] **Step 3c: Insert the gate in each mutating handler**

In each handler, immediately after the skill is located and before the mutation, insert the gate. The `key` is `artifact_key("skill", name)`.

`_edit_skill` (after `_find_skill(name)` at line 552, before the write at 559):

```python
    from agent.automation_ownership import artifact_key
    _err, _pending = _ownership_gate(artifact_key("skill", name))
    if _err:
        return {"error": _err}
```

Add the same four lines to `_patch_skill` (after line 592), `_delete_skill` (after `_pinned_guard` line 685), `_write_file` (after `_find_skill` line 750), and `_remove_file` (after `_find_skill` line 784). Each returns `{"error": _err}` on block.

Then, at the end of each of those handlers, just before the successful `return {...}`, fire the deferred notify:

```python
    if _pending:
        from agent.automation_ownership import record_and_notify
        record_and_notify(*_pending)
```

(For `_create_skill`, no gate — creation can't collide; registration happens in `skill_manage` after success.)

- [ ] **Step 3d: Expose the param in the schema + handler lambda**

In `SKILL_MANAGE_SCHEMA["parameters"]["properties"]` (after the `absorbed_into` property, line 1017), add:

```python
            "confirm_cross_user_owner": {
                "type": "string",
                "description": (
                    "Acknowledge editing a skill owned by another user. Pass the "
                    "owner's name (or id) exactly as shown in the ownership warning, "
                    "and ONLY after the user explicitly confirms. Omit otherwise."
                ),
            },
```

In the `registry.register(... handler=lambda args, **kw: skill_manage(` call (line 1031), add to the kwargs:

```python
        confirm_cross_user_owner=args.get("confirm_cross_user_owner"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_skill_ownership.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Regression — existing skill-manager tests still pass**

Run: `scripts/run_tests.sh tests/tools/ -k "skill"`
Expected: PASS (the gate is inert when ownership is disabled or the editor is the owner/unowned).

- [ ] **Step 6: Commit**

```bash
git add tools/skill_manager_tool.py tests/tools/test_skill_ownership.py
git commit -m "feat(automations): gate cross-user skill edits + register skill creators

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Gate the cron tool (`tools/cronjob_tools.py`)

**Files:**
- Modify: `tools/cronjob_tools.py` (dispatch `cronjob` line 459; create handler ~530; update ~713; remove ~600; pause ~617; schema 724; registry handler lambda 876)
- Test: `tests/tools/test_cron_ownership.py` (create)

**Interfaces:**
- Consumes: `agent.automation_ownership`.
- Produces: `cronjob(..., confirm_cross_user_owner: str = None)` gates `update`/`remove`/`pause` of an owned job and registers the creator on `create`. `list`/`run` are ungated.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_cron_ownership.py`:

```python
"""cronjob enforces the cross-user ownership gate and registers creators."""
import json

import agent.automation_ownership as ao
import agent.data_access_audit as audit
import tools.cronjob_tools as cjt
from agent.automation_ownership import Identity

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _as(identity, monkeypatch):
    monkeypatch.setattr(ao, "current_identity", lambda: identity)
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: True)


def _create_as_alice(monkeypatch):
    _as(ALICE, monkeypatch)
    out = json.loads(cjt.cronjob(action="create", prompt="do a thing", schedule="30m", name="weekly"))
    # The created job id is needed to address it later.
    return out


def test_create_registers_owner(monkeypatch):
    out = _create_as_alice(monkeypatch)
    job_id = out["job"]["id"] if "job" in out else out.get("id")
    assert ao.get_record(f"cron:{job_id}")["owner"]["user_id"] == "U_ALICE"


def test_cross_user_remove_blocked_without_confirm(monkeypatch):
    out = _create_as_alice(monkeypatch)
    job_id = out["job"]["id"] if "job" in out else out.get("id")
    _as(BOB, monkeypatch)
    res = json.loads(cjt.cronjob(action="remove", job_id=job_id))
    assert "error" in res and "owned by Alice" in res["error"]


def test_cross_user_remove_allowed_with_confirm(monkeypatch):
    out = _create_as_alice(monkeypatch)
    job_id = out["job"]["id"] if "job" in out else out.get("id")
    _as(BOB, monkeypatch)
    res = json.loads(cjt.cronjob(action="remove", job_id=job_id, confirm_cross_user_owner="Alice"))
    assert "error" not in res


def test_list_is_ungated(monkeypatch):
    _create_as_alice(monkeypatch)
    _as(BOB, monkeypatch)
    res = json.loads(cjt.cronjob(action="list"))
    assert "error" not in res
```

> Note: if the create response shape differs, read it once from the first test output and adjust the `job_id` extraction; the gating assertions are the point.

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_cron_ownership.py`
Expected: FAIL — Bob's `remove` currently succeeds, and `cronjob` rejects the unexpected `confirm_cross_user_owner` kwarg.

- [ ] **Step 3a: Add `confirm_cross_user_owner` to `cronjob` + gate the mutating actions**

In `tools/cronjob_tools.py`, add `confirm_cross_user_owner: Optional[str] = None` to the `cronjob` signature (line 459 block). Add a small local gate helper at the top of `cronjob`'s body:

```python
    def _gate(job_id_or_ref):
        """(error, pending_notify) for editing an existing job. Fails open."""
        try:
            from agent import automation_ownership as _ao
            from cron.jobs import resolve_job_ref
            if not _ao.is_enabled():
                return None, None
            job = resolve_job_ref(job_id_or_ref)
            if not job:
                return None, None
            key = _ao.artifact_key("cron", job["id"])
            ident = _ao.current_identity()
            res = _ao.check_edit(key, ident, confirm=confirm_cross_user_owner)
            if not res.allowed:
                return res.message, None
            if res.decision == _ao.EditDecision.CROSS_USER and res.record and ident:
                return None, (key, ident, res.record)
            return None, None
        except Exception:
            return None, None
```

In the `remove` handler (line 599–614), `pause` (616–618), and `update` (628–714) branches, immediately at the top of each branch insert:

```python
        _err, _pending = _gate(job_id)
        if _err:
            return json.dumps({"error": _err})
```

and immediately before each of those branches returns its success JSON, add:

```python
        if _pending:
            from agent.automation_ownership import record_and_notify
            record_and_notify(*_pending)
```

(`resume`/`run`/`list`/`create` are not gated.)

- [ ] **Step 3b: Register the creator after a successful `create`**

In the `create` branch, immediately after the `create_job(...)` call returns the new job (line ~530, where the job dict is in hand) and before building the success response, add:

```python
        try:
            from agent import automation_ownership as _ao
            _ao.register_creator(_ao.artifact_key("cron", job["id"]), "cron",
                                 _ao.current_identity())
        except Exception:
            pass
```

(Use whatever local variable holds the created job dict in that branch; it has an `["id"]`.)

- [ ] **Step 3c: Expose the param in the schema + handler lambda**

In `CRONJOB_SCHEMA["parameters"]["properties"]` (after the `profile` property, line 840), add:

```python
            "confirm_cross_user_owner": {
                "type": "string",
                "description": (
                    "Acknowledge editing/removing a cron job owned by another user. "
                    "Pass the owner's name (or id) exactly as shown in the ownership "
                    "warning, and ONLY after the user explicitly confirms. Omit otherwise."
                ),
            },
```

In the `registry.register(... handler=lambda args, **kw: ... cronjob(` call (line 876), add to the kwargs:

```python
        confirm_cross_user_owner=args.get("confirm_cross_user_owner"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_cron_ownership.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Regression — existing cron tests still pass**

Run: `scripts/run_tests.sh tests/tools/ -k "cron" tests/cron/`
Expected: PASS (gate inert when disabled / owner / unowned).

- [ ] **Step 6: Commit**

```bash
git add tools/cronjob_tools.py tests/tools/test_cron_ownership.py
git commit -m "feat(automations): gate cross-user cron edits + register cron creators

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Gate raw file writes to scripts / skills / automations (`tools/file_tools.py`)

**Files:**
- Modify: `tools/file_tools.py` (`write_file_tool` line 1055 — gate alongside the existing `is_protected_data_path` at 1065; `patch_tool` line 1144 — gate in the `_paths_to_check` loop near 1178; schemas at 1467 and 1485; handler registrations)
- Test: `tests/tools/test_file_ownership.py` (create)

**Interfaces:**
- Consumes: `agent.automation_ownership` (`is_enabled`, `current_identity`, `path_to_artifact_key`, `get_record`, `register_creator`, `check_edit`, `record_and_notify`).
- Produces: `write_file_tool(..., confirm_cross_user_owner: str = None)` and `patch_tool(..., confirm_cross_user_owner: str = None)` gate edits to managed automation files; a brand-new file under `scripts/` or `automations/` registers its creator.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_file_ownership.py`:

```python
"""The file tool gates cross-user edits of managed automation files."""
import json

import agent.automation_ownership as ao
import agent.data_access_audit as audit
import tools.file_tools as ft
from agent.automation_ownership import Identity
from hermes_constants import get_hermes_home

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _as(identity, monkeypatch):
    monkeypatch.setattr(ao, "current_identity", lambda: identity)
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: True)


def test_new_script_registers_creator(monkeypatch):
    _as(ALICE, monkeypatch)
    p = get_hermes_home() / "scripts" / "foo.sh"
    out = json.loads(ft.write_file_tool(str(p), "echo hi\n"))
    assert "error" not in out
    assert ao.get_record("script:foo.sh")["owner"]["user_id"] == "U_ALICE"


def test_cross_user_script_patch_blocked(monkeypatch):
    _as(ALICE, monkeypatch)
    p = get_hermes_home() / "scripts" / "foo.sh"
    ft.write_file_tool(str(p), "echo hi\n")
    _as(BOB, monkeypatch)
    out = json.loads(ft.patch_tool(mode="replace", path=str(p),
                                   old_string="echo hi", new_string="echo bye"))
    assert "error" in out and "owned by Alice" in out["error"]


def test_cross_user_script_patch_allowed_with_confirm(monkeypatch):
    _as(ALICE, monkeypatch)
    p = get_hermes_home() / "scripts" / "foo.sh"
    ft.write_file_tool(str(p), "echo hi\n")
    _as(BOB, monkeypatch)
    out = json.loads(ft.patch_tool(mode="replace", path=str(p),
                                   old_string="echo hi", new_string="echo bye",
                                   confirm_cross_user_owner="Alice"))
    assert "error" not in out


def test_unrelated_file_is_untouched(monkeypatch):
    _as(BOB, monkeypatch)
    p = get_hermes_home() / "workspace" / "notes.txt"
    out = json.loads(ft.write_file_tool(str(p), "hello\n"))
    assert "error" not in out  # not a managed automation path -> no gate
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_file_ownership.py`
Expected: FAIL — Bob's patch currently succeeds, and the tools reject the unexpected `confirm_cross_user_owner` kwarg.

- [ ] **Step 3a: Add a shared file-ownership helper**

In `tools/file_tools.py`, add near the other module helpers (top of file, after imports):

```python
def _automation_ownership_check(path, confirm, *, is_write: bool):
    """For a managed automation path, return (error, pending_notify, nudge).

    - error: deny string, or None to allow.
    - pending_notify: (key, editor, record) to fire after a successful confirmed
      cross-user edit, else None.
    - nudge: claim hint to append to a successful result for unowned items, else None.
    Registers the creator when a brand-new managed file is written. Fails open.
    """
    try:
        from agent import automation_ownership as _ao
        if not _ao.is_enabled():
            return None, None, None
        cls = _ao.path_to_artifact_key(path)
        if not cls:
            return None, None, None
        key, kind = cls
        ident = _ao.current_identity()
        from pathlib import Path as _P
        exists = _P(path).expanduser().exists()
        if is_write and not exists and _ao.get_record(key) is None:
            _ao.register_creator(key, kind, ident)
            return None, None, None
        res = _ao.check_edit(key, ident, confirm=confirm)
        if not res.allowed:
            return res.message, None, None
        pending = None
        if res.decision == _ao.EditDecision.CROSS_USER and res.record and ident:
            pending = (key, ident, res.record)
        nudge = res.message if res.decision == _ao.EditDecision.UNOWNED and res.message else None
        return None, pending, nudge
    except Exception:
        return None, None, None
```

- [ ] **Step 3b: Gate `write_file_tool`**

Add `confirm_cross_user_owner: str = None` to the `write_file_tool` signature (line 1055). Immediately after the existing `is_protected_data_path(path)` block (line ~1065–1075), insert:

```python
        _own_err, _own_pending, _own_nudge = _automation_ownership_check(
            path, confirm_cross_user_owner, is_write=True)
        if _own_err:
            return json.dumps({"error": _own_err})
```

After the write succeeds (just before `write_file_tool` returns its success JSON), fire the deferred notify and append the nudge:

```python
        if _own_pending:
            from agent.automation_ownership import record_and_notify
            record_and_notify(*_own_pending)
        # (_own_nudge, if set, may be appended to the human-facing message field)
```

- [ ] **Step 3c: Gate `patch_tool`**

Add `confirm_cross_user_owner: str = None` to the `patch_tool` signature (line 1144). Inside the `for _p in _paths_to_check:` loop (line 1154), immediately after the existing `is_protected_data_path(_p)` check (line ~1178), insert:

```python
        _own_err, _own_pending, _own_nudge = _automation_ownership_check(
            _p, confirm_cross_user_owner, is_write=False)
        if _own_err:
            return tool_error(_own_err)
```

Collect `_own_pending` for the first managed path, and after the patch applies successfully (before `patch_tool` returns), add:

```python
        if _own_pending:
            from agent.automation_ownership import record_and_notify
            record_and_notify(*_own_pending)
```

- [ ] **Step 3d: Expose the param in both schemas + handler lambdas**

In `WRITE_FILE_SCHEMA["parameters"]["properties"]` (after `cross_profile`, line 1479) and `PATCH_SCHEMA["parameters"]["properties"]` (after `cross_profile`, line 1530), add:

```python
            "confirm_cross_user_owner": {
                "type": "string",
                "description": (
                    "Acknowledge editing a script/skill/automation file owned by "
                    "another user. Pass the owner's name (or id) from the ownership "
                    "warning, ONLY after the user explicitly confirms. Omit otherwise."
                ),
            },
```

Then locate the `registry.register` handler lambdas for `write_file` and `patch` in `tools/file_tools.py` (grep `name="write_file"` / `name="patch"` in the registration block lower in the file) and thread the new arg, e.g.:

```python
        confirm_cross_user_owner=args.get("confirm_cross_user_owner"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_file_ownership.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Regression — existing file-tool tests still pass**

Run: `scripts/run_tests.sh tests/tools/test_file_tools.py tests/tools/test_file_tools_protected_data.py`
Expected: PASS (gate inert for non-managed paths and when ownership is disabled).

- [ ] **Step 6: Commit**

```bash
git add tools/file_tools.py tests/tools/test_file_ownership.py
git commit -m "feat(automations): gate cross-user edits of script/skill/automation files

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Always-present system-prompt guidance (`agent/system_prompt.py`)

**Files:**
- Modify: `agent/system_prompt.py` (add `AUTOMATION_OWNERSHIP_GUIDANCE`; append to `stable_parts` in `build_system_prompt_parts`, ~line 133 alongside the other tool-guidance appends)
- Test: `tests/agent/test_automation_ownership_prompt.py` (create)

**Interfaces:**
- Consumes: `agent.automation_ownership.is_enabled`.
- Produces: the guidance string is part of the cached `stable` segment when ownership is enabled and an automation-editing tool is available.

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_automation_ownership_prompt.py`:

```python
"""The ownership guidance is injected into the stable system-prompt segment."""
import types

import agent.automation_ownership as ao
import agent.system_prompt as sp


def _fake_agent(tool_names, model="claude-opus-4-8"):
    return types.SimpleNamespace(
        valid_tool_names=set(tool_names),
        model=model,
        load_soul_identity=False,
        skip_context_files=True,
        _task_completion_guidance=False,
        _tool_use_enforcement="never",
    )


def test_guidance_present_when_enabled_and_tool_available(monkeypatch):
    monkeypatch.setattr(ao, "is_enabled", lambda: True)
    parts = sp.build_system_prompt_parts(_fake_agent({"skill_manage"}))
    assert "owned by" in parts["stable"].lower() or "ownership" in parts["stable"].lower()


def test_guidance_absent_when_disabled(monkeypatch):
    monkeypatch.setattr(ao, "is_enabled", lambda: False)
    parts = sp.build_system_prompt_parts(_fake_agent({"skill_manage"}))
    assert "automation ownership" not in parts["stable"].lower()


def test_guidance_absent_without_editing_tool(monkeypatch):
    monkeypatch.setattr(ao, "is_enabled", lambda: True)
    parts = sp.build_system_prompt_parts(_fake_agent({"web_search"}))
    assert "automation ownership" not in parts["stable"].lower()
```

> Note: `build_system_prompt_parts` reads several attributes off `agent`; the `_fake_agent` namespace above supplies the ones the function touches. If the function accesses an attribute not present, add it to `_fake_agent` (it must be a plain value, not a Mock — no change-detector coupling).

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_prompt.py`
Expected: FAIL — guidance not present.

- [ ] **Step 3a: Add the guidance constant**

In `agent/system_prompt.py`, near the other `*_GUIDANCE` constants, add:

```python
AUTOMATION_OWNERSHIP_GUIDANCE = (
    "AUTOMATION OWNERSHIP: Skills, cron jobs, scripts, and automation bundles may "
    "be owned by a specific teammate. The tools enforce this — when a tool reports "
    "an automation is owned by someone else, relay the warning and get the user's "
    "explicit confirmation, then re-invoke with confirm_cross_user_owner set to the "
    "owner's name. Never confirm on the user's behalf. When a tool reports an "
    "automation is unowned, offer to claim it (`hermes own claim <key>`). Owners and "
    "collaborators edit freely. Build new multi-part automations under "
    "automations/<name>/ via `hermes own init`."
)
```

- [ ] **Step 3b: Append conditionally in `build_system_prompt_parts`**

In `build_system_prompt_parts`, right after the tool-guidance block that appends `MEMORY_GUIDANCE`/`SKILLS_GUIDANCE` (~line 133, after `stable_parts.append(" ".join(tool_guidance))`), add:

```python
    try:
        from agent.automation_ownership import is_enabled as _ao_enabled
        _editing_tools = {"skill_manage", "cronjob", "write_file", "patch"}
        if _ao_enabled() and (_editing_tools & set(agent.valid_tool_names)):
            stable_parts.append(AUTOMATION_OWNERSHIP_GUIDANCE)
    except Exception:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_automation_ownership_prompt.py`
Expected: PASS (3 tests).

- [ ] **Step 5: Regression — existing system-prompt tests still pass**

Run: `scripts/run_tests.sh tests/agent/ -k "system_prompt or prompt_builder"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/system_prompt.py tests/agent/test_automation_ownership_prompt.py
git commit -m "feat(automations): inject ownership guidance into stable system prompt

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `hermes own` CLI + automation bundles (`hermes_cli/own.py`)

**Files:**
- Create: `hermes_cli/own.py`
- Modify: the CLI dispatcher that registers subcommands (find via `grep -rn "add_parser\|subcommand" hermes_cli/__main__.py hermes_cli/cli.py` and register `own` the same way the existing `cron`/`users` commands are)
- Test: `tests/hermes_cli/test_own_cli.py` (create)

**Interfaces:**
- Consumes: `agent.automation_ownership` (`claim`, `transfer`, `add_collaborator`, `remove_collaborator`, `list_for_user`, `register_creator`, `artifact_key`, `Identity`).
- Produces: `run_own(argv: list[str]) -> int` — implements `list | claim | transfer | collab add|remove | init`.

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_own_cli.py`:

```python
"""hermes own: claim / transfer / collaborators / bundle init."""
import agent.automation_ownership as ao
from hermes_cli.own import run_own
from hermes_constants import get_hermes_home


def test_claim_then_list(capsys):
    assert run_own(["claim", "script:foo.sh", "--user", "U_ALICE", "--name", "Alice"]) == 0
    assert ao.get_record("script:foo.sh")["owner"]["user_id"] == "U_ALICE"
    assert run_own(["list", "--user", "U_ALICE"]) == 0
    assert "script:foo.sh" in capsys.readouterr().out


def test_transfer_by_admin():
    run_own(["claim", "cron:j1", "--user", "U_ALICE", "--name", "Alice"])
    assert run_own(["transfer", "cron:j1", "--to", "U_BOB", "--to-name", "Bob", "--admin"]) == 0
    assert ao.get_record("cron:j1")["owner"]["user_id"] == "U_BOB"


def test_collab_add_remove():
    run_own(["claim", "cron:j2", "--user", "U_ALICE", "--name", "Alice"])
    run_own(["collab", "add", "cron:j2", "--user", "U_BOB", "--name", "Bob"])
    assert any(c["user_id"] == "U_BOB" for c in ao.get_record("cron:j2")["collaborators"])
    run_own(["collab", "remove", "cron:j2", "--user", "U_BOB"])
    assert all(c["user_id"] != "U_BOB" for c in ao.get_record("cron:j2")["collaborators"])


def test_init_scaffolds_bundle_and_registers_owner():
    assert run_own(["init", "weekly-report", "--user", "U_ALICE", "--name", "Alice"]) == 0
    base = get_hermes_home() / "automations" / "weekly-report"
    assert (base / "automation.yaml").exists()
    assert (base / "workflow.md").exists()
    assert (base / "scripts").is_dir() and (base / "assets").is_dir()
    assert ao.get_record("automation:weekly-report")["owner"]["user_id"] == "U_ALICE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/hermes_cli/test_own_cli.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_cli.own'`.

- [ ] **Step 3: Create the CLI module**

Create `hermes_cli/own.py`:

```python
"""`hermes own` — manage automation ownership and scaffold automation bundles."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from agent.automation_ownership import (
    Identity, artifact_key, add_collaborator, claim, get_record,
    list_for_user, register_creator, remove_collaborator, transfer,
)
from hermes_constants import get_hermes_home

_BUNDLE_MANIFEST = """\
# Automation bundle manifest
name: {name}
owner: {owner}
description: ""
collaborators: []
links:
  skills: []
  cron_jobs: []
  scripts: []
"""

_BUNDLE_WORKFLOW = """\
# {name}

What this automation does, how the pieces fit together, and how to run it.
"""


def _ident(user: str, name: str | None, platform: str = "slack") -> Identity:
    return Identity(platform=platform, user_id=user, display_name=name or user)


def run_own(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes own")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--user", required=True)

    p_claim = sub.add_parser("claim")
    p_claim.add_argument("key")
    p_claim.add_argument("--user", required=True)
    p_claim.add_argument("--name", default=None)

    p_tr = sub.add_parser("transfer")
    p_tr.add_argument("key")
    p_tr.add_argument("--to", required=True)
    p_tr.add_argument("--to-name", default=None)
    p_tr.add_argument("--by", default="")
    p_tr.add_argument("--by-name", default=None)
    p_tr.add_argument("--admin", action="store_true")

    p_co = sub.add_parser("collab")
    p_co.add_argument("op", choices=["add", "remove"])
    p_co.add_argument("key")
    p_co.add_argument("--user", required=True)
    p_co.add_argument("--name", default=None)

    p_init = sub.add_parser("init")
    p_init.add_argument("name")
    p_init.add_argument("--user", required=True)
    p_init.add_argument("--name", dest="display", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "list":
        out = list_for_user(args.user)
        for k in out["owned"]:
            print(f"owner        {k}")
        for k in out["collaborator"]:
            print(f"collaborator {k}")
        return 0

    if args.cmd == "claim":
        rec = claim(args.key, args.key.split(":", 1)[0], _ident(args.user, args.name))
        print(f"Claimed {args.key} for {rec['owner']['display_name']}")
        return 0

    if args.cmd == "transfer":
        try:
            rec = transfer(
                args.key, _ident(args.to, args.to_name),
                by=_ident(args.by or "cli", args.by_name), by_is_admin=args.admin,
            )
        except (KeyError, PermissionError) as e:
            print(f"Error: {e}")
            return 1
        print(f"Transferred {args.key} to {rec['owner']['display_name']}")
        return 0

    if args.cmd == "collab":
        if get_record(args.key) is None:
            print(f"Error: no ownership record for {args.key}")
            return 1
        if args.op == "add":
            add_collaborator(args.key, _ident(args.user, args.name))
            print(f"Added collaborator {args.user} to {args.key}")
        else:
            remove_collaborator(args.key, args.user)
            print(f"Removed collaborator {args.user} from {args.key}")
        return 0

    if args.cmd == "init":
        base = get_hermes_home() / "automations" / args.name
        (base / "scripts").mkdir(parents=True, exist_ok=True)
        (base / "assets").mkdir(parents=True, exist_ok=True)
        owner = _ident(args.user, args.display)
        (base / "automation.yaml").write_text(
            _BUNDLE_MANIFEST.format(name=args.name, owner=owner.display_name), encoding="utf-8")
        (base / "workflow.md").write_text(
            _BUNDLE_WORKFLOW.format(name=args.name), encoding="utf-8")
        register_creator(artifact_key("automation", args.name), "automation", owner)
        print(f"Initialized automation bundle at {base}")
        return 0

    return 2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/hermes_cli/test_own_cli.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire `own` into the CLI dispatcher**

Find how existing subcommands are registered (`grep -rn "\"cron\"\|'cron'\|users" hermes_cli/__main__.py hermes_cli/cli.py 2>/dev/null`). Register `own` the same way, dispatching to `from hermes_cli.own import run_own; return run_own(remaining_args)`. Then verify:

Run: `hermes own list --user U_ALICE`
Expected: prints nothing (or owned/collaborator lines) and exits 0 — confirms the subcommand is wired.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/own.py tests/hermes_cli/test_own_cli.py
# plus the dispatcher file you modified in Step 5
git commit -m "feat(automations): hermes own CLI + automation bundle scaffolding

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Reference skill + docs + config + full verification

**Files:**
- Create: `skills/automation-ownership/SKILL.md`
- Modify: `CLAUDE.md` (fork-specific subsection)
- Test: full suite + lint + typecheck (no new test file)

**Interfaces:** none (docs + reference content).

- [ ] **Step 1: Add the reference skill**

Create `skills/automation-ownership/SKILL.md`:

```markdown
---
name: automation-ownership
description: Use when building, editing, claiming, or transferring ownership of skills, cron jobs, scripts, or automation bundles in a shared multi-user Hermes — explains the ownership registry, the cross-user edit gate, and the `hermes own` CLI.
metadata:
  hermes:
    tags: [ownership, automations, governance, multi-user]
---

# Automation Ownership

In this shared Hermes, every user-built automation (skill, cron job, script,
automation bundle) can have an **owner** plus **collaborators**, recorded in
`~/.hermes/ownership/registry.json`.

## Rules the tools enforce
- Creating a skill/cron/script/bundle records you as its owner.
- Owners and collaborators edit freely.
- Editing someone else's automation is **gated**: the tool refuses once with a
  warning. Confirm with the user, then re-invoke the same call with
  `confirm_cross_user_owner="<owner name>"`. The owner is DM'd on the confirmed edit.
- Editing an **unowned** legacy automation proceeds, but offer to claim it.
- An autonomous run (no human) may not edit an owned automation.

## `hermes own` CLI
- `hermes own list --user <id>` — what a user owns / collaborates on
- `hermes own claim <key>` — claim an unowned automation (`script:foo.sh`, `cron:<id>`, `skill:<name>`)
- `hermes own transfer <key> --to <id>` — owner or admin reassigns ownership
- `hermes own collab add|remove <key> --user <id>` — manage collaborators
- `hermes own init <name>` — scaffold an automation bundle under `automations/<name>/`

## Building a multi-part automation
Use `hermes own init <name>` to create `automations/<name>/` with `automation.yaml`
(owner/description/links), `workflow.md` (the runbook), `scripts/`, and `assets/`.
```

- [ ] **Step 2: Add a CLAUDE.md subsection**

In `CLAUDE.md`, after the "Cross-user data-access protection" subsection, add:

```markdown
### Automation ownership — [agent/automation_ownership.py](agent/automation_ownership.py)

Records who owns each user-built automation (skill, cron, script, automation
bundle) so a teammate editing another's work hits a code-enforced confirmation
gate, the owner is DM'd on a confirmed edit, and unowned legacy items prompt a
claim (design:
[docs/superpowers/specs/2026-06-30-automation-ownership-design.md](docs/superpowers/specs/2026-06-30-automation-ownership-design.md)).

- **Registry (canonical):** `${HERMES_HOME}/ownership/registry.json`, keyed
  `skill:<name>` / `cron:<job_id>` / `script:<relpath>` / `automation:<bundle>`,
  each `{owner, collaborators, source}`. Atomic writes, profile-aware.
- **Gate (soft, code-enforced):** the `skill_manage`, `cronjob`, and
  `write_file`/`patch` tools call `check_edit` before mutating. A non-owner edit
  is refused until re-invoked with `confirm_cross_user_owner="<owner>"`; **admins
  are not exempt**; autonomous (no-identity) edits of owned items are refused.
  Creating an automation registers the creator as owner. `record_and_notify`
  DMs the owner (via `send_message`) and logs an `automation_edit` event to the
  `data_access_audit` trail. Config under the top-level `automation_ownership:`
  block (`enabled`, default true; `notify_owner`; `registry_path`). **Not a
  security boundary** — RBAC ([gateway/tool_access.py](gateway/tool_access.py))
  remains the real boundary; this is awareness + collaboration.
- **Always-on guidance:** `AUTOMATION_OWNERSHIP_GUIDANCE` is appended to the
  cached `stable` system-prompt segment ([agent/system_prompt.py](agent/system_prompt.py))
  when enabled + an editing tool is available — present every turn, no `SOUL.md` edit.
- **CLI:** `hermes own list|claim|transfer|collab|init` ([hermes_cli/own.py](hermes_cli/own.py));
  `init` scaffolds an optional `${HERMES_HOME}/automations/<name>/` bundle.
```

- [ ] **Step 3: Add a config example**

Append to `config.yaml` (the repo-root example config, untracked) or document in the README the new block:

```yaml
automation_ownership:
  enabled: true          # record owners + gate cross-user edits
  notify_owner: true     # DM the owner on a confirmed cross-user edit
  # registry_path: ~/.hermes/ownership/registry.json   # optional override
```

- [ ] **Step 4: Lint + typecheck + full new-suite run**

Run:
```bash
ruff check .
ty check
scripts/run_tests.sh \
  tests/agent/test_automation_ownership_registry.py \
  tests/agent/test_automation_ownership_decision.py \
  tests/agent/test_automation_ownership_notify.py \
  tests/agent/test_automation_ownership_prompt.py \
  tests/tools/test_skill_ownership.py \
  tests/tools/test_cron_ownership.py \
  tests/tools/test_file_ownership.py \
  tests/hermes_cli/test_own_cli.py
```
Expected: ruff clean, `ty` clean, all ownership tests PASS.

- [ ] **Step 5: Broader regression**

Run:
```bash
scripts/run_tests.sh tests/tools/ tests/agent/ -k "skill or cron or file or system_prompt or ownership"
```
Expected: PASS — no regressions in the touched tools.

- [ ] **Step 6: Commit**

```bash
git add skills/automation-ownership/SKILL.md CLAUDE.md
git commit -m "docs(automations): document ownership feature + reference skill

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Registry (spec §1) → Task 1 (`registry.json`, key scheme, path classification, atomic write, corruption tolerance).
- Ownership module + `check_edit` truth table + mutations (spec §2) → Tasks 1–2 (full decision table incl. owner/collaborator/unowned/cross-user/no-identity; claim/transfer/collaborators; `list_for_user`).
- Enforcement surface / soft gate (spec §3) → Tasks 4 (skills), 5 (cron), 6 (file). Each adds `confirm_cross_user_owner`, registers creators, fires owner notify. Admins are not exempt (no role check anywhere in `check_edit`). No-identity refusal covered in Task 2 tests and enforced by the hooks via `current_identity()` returning `None`.
- Bundles (spec §4) → Task 8 (`hermes own init` scaffolds `automations/<name>/`; `path_to_artifact_key` classifies bundle files in Task 1).
- System-prompt guidance, no SOUL.md edit (spec §5) → Task 7 (stable-segment append, gated on enabled + editing tool).
- Owner notification + audit (spec §6) → Task 3 (`record_and_notify`: `automation_edit` audit event + best-effort DM, fail-open, `notify_owner` honored).
- Config block (spec §7) → Task 1 `_config()` via `read_raw_config`; documented in Task 9. No `gateway/config.py` change needed (matches data-access-audit).
- Ownership management CLI (spec §8) → Task 8 (`list/claim/transfer/collab/init`; transfer = owner or `--admin`).

**Placeholder scan:** No TBD/TODO. Every code step shows complete code. Commands have expected output. Two spots intentionally instruct a `grep` to locate a registration/dispatcher site (Task 6 Step 3d handler lambdas, Task 8 Step 5 CLI dispatcher) because those exact lines weren't pinned during research — each gives the exact grep and the exact line to add; this is discovery of a known-shaped site, not an unresolved design gap.

**Type consistency:** `Identity(platform, user_id, display_name)` used uniformly across all tasks. `check_edit(key, identity, *, confirm=None) -> EditResult` and `EditResult.{decision, allowed, message, record}` match every hook call site (Tasks 4–6). `EditDecision.CROSS_USER`/`UNOWNED`/`NO_IDENTITY` referenced consistently. `register_creator(key, kind, identity)`, `artifact_key(kind, stable_id)`, `path_to_artifact_key(path) -> (key, kind)|None`, `record_and_notify(key, editor, record)` signatures match across producers (Tasks 1–3) and consumers (Tasks 4–8). The `confirm_cross_user_owner` param name is identical in all three schemas, all three function signatures, and all three handler lambdas.

**Note on the confirm mechanism:** Tasks 4 and 6 carry the confirm token differently — Task 4 uses a module contextvar (`_CONFIRM_OWNER`) to avoid threading it through six skill handlers, while Tasks 5–6 pass it as a local closure variable because those tools have a single dispatch body. Both resolve to the same `check_edit(..., confirm=...)` call; the difference is purely how the token reaches the gate within each tool's existing structure.
