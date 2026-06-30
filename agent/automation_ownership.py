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


def check_edit(key: str, identity: Optional[Identity], *, confirm: Optional[str] = None) -> "EditResult":
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
    if get_record(key) is not None:
        raise PermissionError(
            f"{key} is already owned; use `hermes own transfer {key} --to <id>` to reassign."
        )
    rec = {
        "kind": kind,
        "owner": _ident_dict(identity),
        "collaborators": [],
        "source": "claim",
    }
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
            target=key,
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
