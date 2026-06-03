"""slack_react — let the agent add/remove a Slack emoji reaction.

Mirrors ``tools/send_message_tool.py:_send_slack``: a fresh aiohttp session
posted to the Slack Web API, so the call is safe from any event loop (the tool
runs in a worker thread bridged by the registry's ``_run_async``). Targets the
triggering message via session contextvars unless an explicit message_id is
given. Intended for quiet/observer Slack channels (emoji-first), but usable in
any Slack channel.
"""

from __future__ import annotations

import os

from tools.registry import registry, tool_error, tool_result


SLACK_REACT_SCHEMA = {
    "name": "slack_react",
    "description": (
        "Add (or remove) an emoji reaction on a Slack message. By default it "
        "reacts to the message that triggered the current turn — ideal for "
        "acknowledging or signaling completion without posting a text reply. "
        "Provide the emoji by its Slack short name WITHOUT colons "
        "(e.g. 'white_check_mark', 'eyes', 'party_sloth')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": "Slack emoji short name without colons, e.g. 'party_sloth'.",
            },
            "message_id": {
                "type": "string",
                "description": "Target message timestamp (ts). Defaults to the triggering message.",
            },
            "remove": {
                "type": "boolean",
                "description": "Remove the reaction instead of adding it. Defaults to false.",
                "default": False,
            },
        },
        "required": ["emoji"],
    },
}


def _session(name: str, default: str = "") -> str:
    """Thin wrapper around session context (indirection for tests)."""
    from gateway.session_context import get_session_env
    return get_session_env(name, default)


def _resolve_slack_token() -> str:
    """Resolve the Slack bot token from gateway config, falling back to env."""
    try:
        from gateway.config import load_gateway_config, Platform
        cfg = load_gateway_config()
        pconfig = cfg.platforms.get(Platform.SLACK)
        if pconfig and getattr(pconfig, "token", ""):
            return pconfig.token or ""
    except Exception:
        pass
    return os.getenv("SLACK_BOT_TOKEN", "").strip()


async def _post_reaction(token: str, channel: str, ts: str, emoji: str, remove: bool) -> dict:
    """POST to Slack reactions.add / reactions.remove. Returns parsed JSON."""
    import aiohttp
    from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

    method = "reactions.remove" if remove else "reactions.add"
    url = f"https://slack.com/api/{method}"
    _proxy = resolve_proxy_url()
    _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "timestamp": ts, "name": emoji}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), **_sess_kw) as session:
        async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
            return await resp.json(content_type=None)


async def _slack_react_handler(args: dict, **_kw) -> str:
    emoji = (args.get("emoji") or "").strip().strip(":")
    if not emoji:
        return tool_error("'emoji' is required (Slack short name without colons).")

    channel = _session("HERMES_SESSION_CHAT_ID").strip()
    ts = (args.get("message_id") or _session("HERMES_SESSION_MESSAGE_ID")).strip()
    if not channel or not ts:
        return tool_error(
            "No target Slack message in context. slack_react only works on a live "
            "Slack turn (or pass an explicit message_id)."
        )

    token = _resolve_slack_token()
    if not token:
        return tool_error("Slack bot token not configured (SLACK_BOT_TOKEN).")

    remove = bool(args.get("remove", False))
    try:
        data = await _post_reaction(token, channel, ts, emoji, remove)
    except Exception as e:
        return tool_error(f"Slack reaction request failed: {e}")

    if data.get("ok"):
        return tool_result(success=True, emoji=emoji, channel=channel, ts=ts, removed=remove)
    err = data.get("error", "unknown")
    if err in {"already_reacted", "no_reaction"}:
        return tool_result(success=True, emoji=emoji, noop=err)
    return tool_error(f"Slack API error: {err}")


def _check_slack_react() -> bool:
    """Available whenever a Slack token is resolvable."""
    return bool(_resolve_slack_token())


registry.register(
    name="slack_react",
    toolset="slack",
    schema=SLACK_REACT_SCHEMA,
    handler=lambda args, **kw: _slack_react_handler(args, **kw),
    check_fn=_check_slack_react,
    requires_env=[],
    is_async=True,
    emoji="🦥",
    max_result_size_chars=2000,
)
