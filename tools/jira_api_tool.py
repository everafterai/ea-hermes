"""jira_api — a thin, structured proxy over the JIRA Cloud REST API (read-only).

The model calls ``jira_api(path, method)`` and gets parsed JSON back. Mirrors
``notion_api``'s ergonomics, but talks HTTP directly (no CLI dependency) using
Atlassian Cloud basic auth (email:API_TOKEN, base64) against JIRA_BASE_URL. MVP
is GET-only — the reconciliation worker only reads. Credentials live in
~/.hermes/.env (JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN) and self-heal for
headless cron/worker runs that never loaded the dotenv (mirrors slack_react).
"""
from __future__ import annotations

import base64
import json
import logging
import os

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_ALLOWED_METHODS = frozenset({"GET"})
_JIRA_TIMEOUT_SECONDS = 30


JIRA_API_SCHEMA = {
    "name": "jira_api",
    "description": (
        "Call the JIRA Cloud REST API (READ-ONLY) and get parsed JSON back. Pass "
        "an API path WITHOUT a leading slash, e.g. "
        "'rest/api/3/issue/EA-123?fields=status' or "
        "'rest/api/3/search/jql?jql=key IN (EA-1,EA-2)&fields=status'. Only GET "
        "is supported. Use this for ALL JIRA reads — never shell out or use curl."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "JIRA REST path beginning with 'rest/', no leading slash.",
            },
            "method": {
                "type": "string",
                "enum": ["GET"],
                "description": "HTTP method. Only GET is supported.",
                "default": "GET",
            },
        },
        "required": ["path"],
    },
}


def _jira_creds() -> tuple[str, str, str]:
    """Resolve (base_url, email, token), self-healing from ~/.hermes/.env once.

    A headless cron/worker process may never have loaded the dotenv, so load it
    if any var is missing (mirrors notion_api/_ntn_env and slack_react).
    """
    def _read() -> tuple[str, str, str]:
        return (
            os.getenv("JIRA_BASE_URL", "").strip().rstrip("/"),
            os.getenv("JIRA_EMAIL", "").strip(),
            os.getenv("JIRA_API_TOKEN", "").strip(),
        )

    base, email, token = _read()
    if not (base and email and token):
        try:
            from hermes_cli.env_loader import load_hermes_dotenv
            load_hermes_dotenv()
        except Exception:
            pass
        base, email, token = _read()
    return base, email, token


async def _jira_request(base_url: str, email: str, token: str, path: str) -> dict:
    """GET base_url/path with Atlassian basic auth. Returns {status, text}.

    The raw path (incl. an un-encoded ``?jql=key IN (...)`` query) is handed to
    aiohttp/yarl, which percent-encodes it.
    """
    import aiohttp
    from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

    url = f"{base_url}/{path.lstrip('/')}"
    cred = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {cred}", "Accept": "application/json"}
    _proxy = resolve_proxy_url()
    _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
    timeout = aiohttp.ClientTimeout(total=_JIRA_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, **_sess_kw) as session:
        async with session.get(url, headers=headers, **_req_kw) as resp:
            return {"status": resp.status, "text": await resp.text()}


async def _jira_api_handler(args: dict, **_kw) -> str:
    path = (args.get("path") or "").strip()
    method = (args.get("method") or "GET").strip().upper()

    if method not in _ALLOWED_METHODS:
        return tool_error(f"Unsupported method '{method}'. Only GET is allowed.")
    if not path.startswith("rest/"):
        return tool_error("path must be a JIRA REST path beginning with 'rest/'.")

    base, email, token = _jira_creds()
    if not (base and email and token):
        return tool_error(
            "JIRA not configured (set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)."
        )

    try:
        resp = await _jira_request(base, email, token, path)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[jira_api] request failed (GET %s): %s", path, e)
        return tool_error(f"JIRA request failed: {e}")

    status = resp["status"]
    body = (resp["text"] or "").strip()
    if status >= 400:
        logger.warning("[jira_api] HTTP %s (GET %s): %s", status, path, body[:300])
        return tool_error(f"JIRA API error (HTTP {status}): {body[:300]}")
    if not body:
        return tool_result({"ok": True, "data": None})
    try:
        data = json.loads(body)
    except ValueError:
        return tool_result({"ok": True, "text": body})
    return tool_result({"ok": True, "data": data})


def _check_jira_api() -> bool:
    """Available whenever all three JIRA credentials are resolvable."""
    base, email, token = _jira_creds()
    return bool(base and email and token)


registry.register(
    name="jira_api",
    toolset="jira",
    schema=JIRA_API_SCHEMA,
    handler=lambda args, **kw: _jira_api_handler(args, **kw),
    check_fn=_check_jira_api,
    requires_env=[],
    is_async=True,
    emoji="🎫",
    max_result_size_chars=16000,
)
