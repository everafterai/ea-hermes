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
