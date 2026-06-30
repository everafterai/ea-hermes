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
