"""Platform user id → Google Workspace email, for the Drive access check.

Lookup order: ``<platform>.user_emails`` in config.yaml → (Slack only)
``users.info`` → write the answer back into ``slack.user_emails`` so the next
turn — and the next gateway restart — never asks Slack again. A resolved
email is always cached in-process regardless of whether the write-back
succeeded, so a read-only config can't cause a Slack call per turn.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_SLACK_USERS_INFO = "https://slack.com/api/users.info"

_lock = threading.Lock()
_cache: dict[tuple[str, str], str] = {}


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _raw_config() -> dict:
    """Seam: the raw ``config.yaml`` dict (cached on mtime by hermes_cli)."""
    from hermes_cli.config import read_raw_config_readonly

    return read_raw_config_readonly() or {}


def _lookup_config(platform: str, user_id: str) -> Optional[str]:
    try:
        block = _raw_config().get(platform) or {}
        emails = block.get("user_emails") if isinstance(block, dict) else None
        if not isinstance(emails, dict):
            return None
        for k, v in emails.items():
            if str(k) == user_id and v:
                return str(v).strip().lower() or None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("user_emails lookup failed: %s", exc)
    return None


def _slack_users_info(user_id: str) -> dict:
    """Seam: GET users.info. Returns the parsed JSON body (``{}`` if no token)."""
    from tools.slack_react_tool import _resolve_slack_token

    token = _resolve_slack_token()
    if not token:
        return {}
    import httpx
    from gateway.platforms.base import resolve_proxy_url

    proxy = resolve_proxy_url() or None
    with httpx.Client(timeout=10.0, proxy=proxy) as http:
        resp = http.get(
            _SLACK_USERS_INFO,
            params={"user": user_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        return resp.json()


def _lookup_slack(user_id: str) -> Optional[str]:
    try:
        data = _slack_users_info(user_id) or {}
    except Exception as exc:
        logger.warning("Slack users.info(%s) failed: %s", user_id, exc)
        return None
    if not data.get("ok"):
        err = data.get("error") or "no token"
        hint = " (add the users:read.email bot scope and reinstall the app)" if err in (
            "missing_scope", "no token"
        ) else ""
        logger.warning(
            "Slack users.info(%s) returned %s%s — cannot resolve email; "
            "set slack.user_emails.%s manually or fix the scope (users:read.email)",
            user_id, err, hint, user_id,
        )
        return None
    email = ((data.get("user") or {}).get("profile") or {}).get("email")
    email = str(email or "").strip().lower()
    return email or None


def _persist_email(platform: str, user_id: str, email: str) -> None:
    """Write ``user_id → email`` into ``<platform>.user_emails`` (Slack only in v1).

    Reuses the comment-preserving writer from ``hermes users``. Raises on
    failure — the caller logs and keeps the in-memory value.
    """
    if platform != "slack":
        return
    from hermes_cli.users import _mutate_slack, apply_set_email

    _mutate_slack(lambda extra: apply_set_email(extra, user_id, email))


def resolve_email(platform: str, user_id: str) -> Optional[str]:
    platform = str(platform or "").strip().lower()
    user_id = str(user_id or "").strip()
    if not platform or not user_id:
        return None
    key = (platform, user_id)
    with _lock:
        cached = _cache.get(key)
    if cached:
        return cached

    email = _lookup_config(platform, user_id)
    persisted = True
    if not email and platform == "slack":
        email = _lookup_slack(user_id)
        persisted = False
    if not email:
        return None

    with _lock:
        _cache[key] = email
    if not persisted:
        try:
            _persist_email(platform, user_id, email)
        except Exception as exc:
            logger.warning(
                "could not write slack.user_emails.%s to config.yaml (%s); "
                "using the in-memory value for this process", user_id, exc,
            )
    return email
