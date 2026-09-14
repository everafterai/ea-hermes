"""Per-user access check for the Drive / Sheets / Docs tools.

The plugin acts as ONE service account, so "shared with the SA" would
otherwise mean "readable by every user whose role grants the toolset". This
module makes each tool call answer to the *requesting* user instead: resolve
who is asking (session contextvars, or the cron job's owner), fetch the
target file's ACL as the SA, and check that user is on it. Google Groups
cannot be expanded without domain-wide delegation, so a group grant counts
only when the operator has mapped it in config (``everyone_groups`` /
``group_members``); anything else fails closed and is reported to the audit
log so the operator can see which group to map.

Design: docs/superpowers/specs/2026-09-14-drive-per-user-access-check-design.md
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

READER = "reader"
WRITER = "writer"

# Drive roles that satisfy each access level. ``owner``/``organizer``/
# ``fileOrganizer`` are shared-drive and My-Drive spellings of "can edit".
_WRITER_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer"})
_READER_ROLES = _WRITER_ROLES | frozenset({"commenter", "reader"})
# Rank for "highest role wins" — higher is more privileged.
_ROLE_RANK = {
    "reader": 1,
    "commenter": 2,
    "writer": 3,
    "fileOrganizer": 4,
    "organizer": 5,
    "owner": 6,
}

_DEFAULT_TTL = 300.0


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AccessConfig:
    enabled: bool = True
    cache_ttl: float = _DEFAULT_TTL
    everyone_groups: frozenset = frozenset()
    group_members: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.group_members is None:
            object.__setattr__(self, "group_members", {})


def _raw_config() -> dict:
    """Seam: the raw ``config.yaml`` dict. Tests monkeypatch this."""
    from hermes_cli.config import read_raw_config_readonly

    return read_raw_config_readonly() or {}


def _lower_str(value: Any) -> str:
    return str(value or "").strip().lower()


def load_access_config() -> AccessConfig:
    """Read the top-level ``google_drive:`` block. Defaults on any failure."""
    try:
        block = _raw_config().get("google_drive") or {}
        if not isinstance(block, dict):
            block = {}
        enabled = bool(block.get("access_check", True))
        try:
            ttl = float(block.get("acl_cache_ttl_seconds", _DEFAULT_TTL))
        except (TypeError, ValueError):
            ttl = _DEFAULT_TTL
        raw_everyone = block.get("everyone_groups") or []
        everyone = frozenset(
            _lower_str(g) for g in raw_everyone if _lower_str(g)
        ) if isinstance(raw_everyone, (list, tuple, set)) else frozenset()
        members: dict[str, frozenset] = {}
        raw_members = block.get("group_members") or {}
        if isinstance(raw_members, dict):
            for group, emails in raw_members.items():
                if not isinstance(emails, (list, tuple, set)):
                    continue
                key = _lower_str(group)
                if key:
                    members[key] = frozenset(
                        _lower_str(e) for e in emails if _lower_str(e)
                    )
        return AccessConfig(
            enabled=enabled,
            cache_ttl=ttl,
            everyone_groups=everyone,
            group_members=members,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("google_drive access config unreadable (%s); using defaults", exc)
        return AccessConfig()


# --------------------------------------------------------------------------- #
# Evaluation (pure)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Decision:
    granted_role: Optional[str]
    unmapped_groups: tuple = ()

    def satisfies(self, level: str) -> bool:
        if self.granted_role is None:
            return False
        if level == WRITER:
            return self.granted_role in _WRITER_ROLES
        return self.granted_role in _READER_ROLES


def evaluate(acl: list, email: str, cfg: AccessConfig) -> Decision:
    """Pure ACL evaluation for one requester. Never raises on bad entries."""
    e = _lower_str(email)
    domain = e.rsplit("@", 1)[1] if "@" in e else ""
    best: Optional[str] = None
    unmapped: list[str] = []
    for entry in acl or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "")
        if role not in _ROLE_RANK:
            continue
        kind = str(entry.get("type") or "")
        matched = False
        if kind == "user":
            matched = _lower_str(entry.get("emailAddress")) == e and bool(e)
        elif kind == "domain":
            matched = bool(domain) and _lower_str(entry.get("domain")) == domain
        elif kind == "anyone":
            matched = True
        elif kind == "group":
            group = _lower_str(entry.get("emailAddress"))
            if group in cfg.everyone_groups:
                matched = True
            elif group in cfg.group_members:
                matched = e in cfg.group_members[group]
            elif group and group not in unmapped:
                unmapped.append(group)
        if matched and (best is None or _ROLE_RANK[role] > _ROLE_RANK[best]):
            best = role
    return Decision(granted_role=best, unmapped_groups=tuple(unmapped))
