"""Attribute Hermes spend to Slack channels and cron jobs (fork-only).

Pure query layer over ``SessionDB``: nothing here writes except ``reprice``.
Attribution is derived at query time from columns that already exist —
``parent_session_id`` (lineage), the ``cron_<job>_<ts>`` session id (job),
and ``origin_json`` (channel). See
docs/superpowers/specs/2026-09-17-cost-attribution-design.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

_CRON_ID_RE = re.compile(r"^cron_(?P<job>.+)_\d{8}_\d{6}$")

PRICED_STATUSES = frozenset({"actual", "estimated", "included"})


def cron_job_id_from_session_id(session_id: Optional[str]) -> Optional[str]:
    """Return the job id embedded in a cron root session id, else None."""
    if not session_id:
        return None
    match = _CRON_ID_RE.match(session_id)
    return match.group("job") if match else None


@dataclass(frozen=True)
class ChannelKey:
    platform: str
    chat_id: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.platform}:{self.name or self.chat_id}"


def _parse_origin(origin_json: Optional[str]) -> Dict[str, Any]:
    if not origin_json:
        return {}
    try:
        data = json.loads(origin_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_thread_origin(origin_json: Optional[str]) -> bool:
    return bool(_parse_origin(origin_json).get("parent_chat_id"))


def channel_from_origin(
    *,
    source: Optional[str],
    chat_id: Optional[str],
    chat_type: Optional[str],
    user_id: Optional[str],
    origin_json: Optional[str],
    display_name: Optional[str],
) -> Optional[ChannelKey]:
    """Resolve the channel a session belongs to; threads collapse to their parent."""
    origin = _parse_origin(origin_json)
    platform = str(origin.get("platform") or source or "").strip()
    resolved_chat = origin.get("parent_chat_id") or origin.get("chat_id") or chat_id
    resolved_type = origin.get("chat_type") or chat_type
    if not platform or not resolved_chat:
        return None
    resolved_chat = str(resolved_chat)
    if resolved_type == "dm":
        dm_key = f"dm:{user_id or resolved_chat}"
        return ChannelKey(platform=platform, chat_id=dm_key, name=dm_key)
    name = origin.get("chat_name") or display_name or resolved_chat
    return ChannelKey(platform=platform, chat_id=resolved_chat, name=str(name))
