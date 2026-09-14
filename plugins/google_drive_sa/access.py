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


# --------------------------------------------------------------------------- #
# Requester
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Requester:
    platform: str
    user_id: str
    email: str


def _engaged() -> bool:
    """Seam: has any session been bound in this process (gateway/cron)?"""
    from gateway.session_context import session_context_engaged

    return session_context_engaged()


def _resolve_email(platform: str, user_id: str) -> Optional[str]:
    from plugins.google_drive_sa.identity import resolve_email

    return resolve_email(platform, user_id)


def _cron_owner() -> Optional[tuple]:
    """``(platform, user_id)`` of the running cron job's owner, else None."""
    try:
        from cron.tool_approval_context import get_cron_job_id

        job_id = get_cron_job_id()
        if not job_id:
            return None
        from agent import automation_ownership as ao

        record = ao.get_record(ao.artifact_key("cron", job_id)) or {}
        owner = record.get("owner") or {}
        platform = str(owner.get("platform") or "").strip()
        user_id = str(owner.get("user_id") or "").strip()
        if platform and user_id:
            return (platform, user_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cron owner lookup failed: %s", exc)
    return None


def is_check_active() -> bool:
    """The check runs only inside a gateway/cron process with it enabled.

    A plain CLI (never engaged the session-context system) is the operator's
    own shell and skips it — the fork's "a shell caller is an admin" rule.
    """
    return load_access_config().enabled and _engaged()


def resolve_requester() -> Optional[Requester]:
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip()
    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    if not (platform and user_id):
        owner = _cron_owner()
        if owner is None:
            return None
        platform, user_id = owner
    email = _resolve_email(platform, user_id)
    if not email:
        return None
    return Requester(platform=platform, user_id=user_id, email=email.lower())


# --------------------------------------------------------------------------- #
# ACL fetch + cache
# --------------------------------------------------------------------------- #

_PERM_FIELDS = "permissions(type,emailAddress,domain,role)"
_GET_FIELDS = f"id,name,driveId,{_PERM_FIELDS}"

_cache_lock = threading.Lock()
# file_id -> (fetched_at, name, acl)
_acl_cache: dict[str, tuple] = {}


def _now() -> float:
    return time.monotonic()


def reset_cache() -> None:
    with _cache_lock:
        _acl_cache.clear()


def _cache_put(file_id: str, name: str, acl: list) -> None:
    with _cache_lock:
        _acl_cache[file_id] = (_now(), name, list(acl))


def _cache_get(file_id: str) -> Optional[tuple]:
    ttl = load_access_config().cache_ttl
    with _cache_lock:
        hit = _acl_cache.get(file_id)
    if hit is None:
        return None
    fetched_at, name, acl = hit
    if _now() - fetched_at > ttl:
        return None
    return name, acl


def _list_permissions(svc: Any, file_id: str) -> list:
    acl: list = []
    token: Optional[str] = None
    while True:
        kw: dict[str, Any] = dict(
            fileId=file_id,
            supportsAllDrives=True,
            pageSize=100,
            fields=f"nextPageToken,{_PERM_FIELDS}",
        )
        if token:
            kw["pageToken"] = token
        resp = svc.permissions().list(**kw).execute() or {}
        acl.extend(resp.get("permissions") or [])
        token = resp.get("nextPageToken")
        if not token:
            return acl


def fetch_acl(file_id: str) -> tuple:
    """``(name, acl)`` for *file_id*, cached. Raises on any API failure.

    ``files.get`` returns the ACL inline for My Drive files the SA can share;
    shared-drive items (and files the SA can only read) come back without
    ``permissions`` and need ``permissions.list`` — which also includes the
    drive-level memberships shared-drive access is usually granted through.
    """
    cached = _cache_get(file_id)
    if cached is not None:
        return cached
    from plugins.google_drive_sa import client

    svc = client.get_service()
    meta = svc.files().get(fileId=file_id, fields=_GET_FIELDS, supportsAllDrives=True).execute() or {}
    name = str(meta.get("name") or "")
    acl = meta.get("permissions")
    if acl is None:
        acl = _list_permissions(svc, file_id)
    _cache_put(file_id, name, acl)
    return name, list(acl)


# --------------------------------------------------------------------------- #
# require_access
# --------------------------------------------------------------------------- #

class DriveAccessDenied(Exception):
    """Raised by require_access; ``str(exc)`` is safe to show the model."""


def _audit_denied(
    *, file_id: str, name: str, level: str, requester: str,
    granted_role: Optional[str], unmapped_groups: tuple, reason: str,
) -> None:
    """Audit-log a denial with the detail the tool result deliberately omits."""
    try:
        from agent.data_access_audit import record_access

        record_access(
            tool="google_drive",
            action="drive_access_denied",
            target=(
                f"drive:{file_id} level={level} requester={requester or '-'} "
                f"granted={granted_role or '-'} reason={reason} "
                f"unmapped_groups={','.join(unmapped_groups) or '-'} "
                f"name={name[:80]!r}"
            ),
        )
    except Exception:  # pragma: no cover - auditing never breaks a tool
        pass


def _denial_text(email: str, level: str) -> str:
    verb = "edit" if level == WRITER else "access"
    return (
        f"Access denied: {email} does not have permission to {verb} this file. "
        "Ask the file's owner to share it with you (or with a group the "
        "operator has mapped), then try again."
    )


def require_access(file_id: str, level: str) -> Optional[Requester]:
    """Gate one tool call. Returns the requester, or None if the check is off.

    Raises :class:`DriveAccessDenied` when the requester cannot be resolved,
    the ACL cannot be fetched, or the ACL does not grant *level*.
    """
    if not is_check_active():
        return None
    requester = resolve_requester()
    if requester is None:
        _audit_denied(
            file_id=file_id, name="", level=level, requester="",
            granted_role=None, unmapped_groups=(), reason="no_requester",
        )
        raise DriveAccessDenied(
            "Access denied: the requesting user could not be identified, so "
            "Drive access cannot be verified. (No platform identity in this "
            "session, or no email is mapped for it — an operator can set "
            "slack.user_emails.)"
        )
    try:
        name, acl = fetch_acl(file_id)
    except Exception as exc:
        logger.warning("ACL fetch for %s failed (%s); denying", file_id, exc)
        _audit_denied(
            file_id=file_id, name="", level=level, requester=requester.email,
            granted_role=None, unmapped_groups=(), reason="acl_fetch_failed",
        )
        raise DriveAccessDenied(
            f"Access denied: could not verify {requester.email}'s access to this "
            f"file ({type(exc).__name__}). Check the file ID, or that the file "
            "is shared with the service account."
        ) from exc
    decision = evaluate(acl, requester.email, load_access_config())
    if decision.satisfies(level):
        return requester
    _audit_denied(
        file_id=file_id, name=name, level=level, requester=requester.email,
        granted_role=decision.granted_role, unmapped_groups=decision.unmapped_groups,
        reason="denied",
    )
    raise DriveAccessDenied(_denial_text(requester.email, level))


# --------------------------------------------------------------------------- #
# share_with_requester
# --------------------------------------------------------------------------- #

def share_with_requester(file_id: str, requester: Optional[Requester]) -> Optional[str]:
    """Add *requester* as writer on a file the SA just created in its own root.

    Without this, nobody but the SA is on the new file's ACL and the person
    who asked for it could not read it back. Returns an error string on
    failure (the file exists; the caller reports it), None on success/no-op.
    """
    if requester is None:
        return None
    try:
        from plugins.google_drive_sa import client

        client.get_service().permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "writer", "emailAddress": requester.email},
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ).execute()
        with _cache_lock:
            _acl_cache.pop(file_id, None)
        return None
    except Exception as exc:
        logger.warning("could not share %s with %s: %s", file_id, requester.email, exc)
        return (
            f"File created, but sharing it with {requester.email} failed "
            f"({type(exc).__name__}: {exc}); they may not be able to open it."
        )


# --------------------------------------------------------------------------- #
# Listing filter
# --------------------------------------------------------------------------- #

_LIST_POOL_WORKERS = 8
# Persistent pool (not one-per-call) so worker threads — and the per-thread
# googleapiclient service each one builds (see client.py's module docstring)
# — are reused across listings instead of rebuilt every call.
_LIST_POOL = ThreadPoolExecutor(max_workers=_LIST_POOL_WORKERS, thread_name_prefix="gdrive-acl")


def _strip_acl(files: list) -> list:
    out = []
    for f in files:
        if isinstance(f, dict):
            f = {k: v for k, v in f.items() if k != "permissions"}
        out.append(f)
    return out


def filter_listing(files: list, level: str = READER) -> list:
    """Drop listed files the requester cannot access at *level*.

    Files that came back with an inline ``permissions`` array are evaluated
    in place (and prime the ACL cache). The rest — shared-drive items, or
    files the SA can't share — are fetched in a small thread pool. A file
    whose ACL cannot be fetched is dropped (fail closed). No count of hidden
    items is exposed anywhere.
    """
    if not is_check_active():
        return _strip_acl(files)
    requester = resolve_requester()
    if requester is None:
        return []
    cfg = load_access_config()

    def _decide(f: dict) -> bool:
        file_id = str(f.get("id") or "")
        if not file_id:
            return False
        acl = f.get("permissions")
        if acl is None:
            try:
                _, acl = fetch_acl(file_id)
            except Exception as exc:
                logger.debug("listing: ACL fetch for %s failed (%s); hiding", file_id, exc)
                return False
        else:
            _cache_put(file_id, str(f.get("name") or ""), acl)
        return evaluate(acl, requester.email, cfg).satisfies(level)

    inline = [(i, f) for i, f in enumerate(files) if isinstance(f, dict) and f.get("permissions") is not None]
    remote = [(i, f) for i, f in enumerate(files) if isinstance(f, dict) and f.get("permissions") is None]
    allowed: set = set()
    for i, f in inline:
        if _decide(f):
            allowed.add(i)
    if remote:
        remote_indices = [i for i, _ in remote]
        remote_files = [f for _, f in remote]
        for i, ok in zip(remote_indices, list(_LIST_POOL.map(_decide, remote_files))):
            if ok:
                allowed.add(i)
    return _strip_acl([f for i, f in enumerate(files) if i in allowed])
