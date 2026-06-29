"""slack_post_thread — post one text message into a specific Slack thread.

A headless-safe sibling of ``slack_react``: it takes an EXPLICIT chat_id +
thread_ts (no session contextvars) so it works from cron/worker runs, and posts
via Slack ``chat.postMessage``. It reuses ``slack_react``'s token self-heal. It
lives in its own NON-FLOOR ``slack_post`` toolset (deliberately NOT the floor
``slack`` toolset, which would hand every valid-role user arbitrary
thread-posting). This is the worker's poster because cron hard-disables the
``messaging`` toolset that ``send_message`` lives in.
"""
from __future__ import annotations

import logging

from tools.registry import registry, tool_error, tool_result
from tools.slack_react_tool import _resolve_slack_token

logger = logging.getLogger(__name__)


SLACK_POST_THREAD_SCHEMA = {
    "name": "slack_post_thread",
    "description": (
        "Post a text message into a specific Slack thread. Requires the channel "
        "id, the thread's root timestamp (thread_ts), and the message text "
        "(Slack mrkdwn). Use to deliver an update into a known thread from a "
        "background/cron run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Slack channel id, e.g. 'C0123ABCD'.",
            },
            "thread_ts": {
                "type": "string",
                "description": "Root message ts of the thread, e.g. '1700000000.000100'.",
            },
            "message": {
                "type": "string",
                "description": "Message text (Slack mrkdwn).",
            },
        },
        "required": ["chat_id", "thread_ts", "message"],
    },
}


async def _post_message(token: str, channel: str, thread_ts: str, text: str) -> dict:
    """POST to Slack chat.postMessage in a thread. Returns parsed JSON.

    Mirrors slack_react._post_reaction: a fresh aiohttp session with proxy
    support, safe from any event loop.
    """
    import aiohttp
    from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

    url = "https://slack.com/api/chat.postMessage"
    _proxy = resolve_proxy_url()
    _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "thread_ts": thread_ts, "text": text}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), **_sess_kw) as session:
        async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
            return await resp.json(content_type=None)


async def _slack_post_thread_handler(args: dict, **_kw) -> str:
    channel = (args.get("chat_id") or "").strip()
    thread_ts = (args.get("thread_ts") or "").strip()
    message = (args.get("message") or "").strip()
    if not channel or not thread_ts or not message:
        return tool_error("'chat_id', 'thread_ts', and 'message' are all required.")

    token = _resolve_slack_token()
    if not token:
        logger.warning("[slack_post_thread] no Slack bot token resolved (channel=%s)", channel)
        return tool_error("Slack bot token not configured (SLACK_BOT_TOKEN).")

    try:
        data = await _post_message(token, channel, thread_ts, message)
    except Exception as e:
        logger.warning("[slack_post_thread] request failed (channel=%s ts=%s): %s",
                       channel, thread_ts, e)
        return tool_error(f"Slack post request failed: {e}")

    if data.get("ok"):
        return tool_result(success=True, channel=channel,
                           thread_ts=data.get("ts", thread_ts))
    err = data.get("error", "unknown")
    logger.warning("[slack_post_thread] Slack API error '%s' (channel=%s ts=%s)",
                   err, channel, thread_ts)
    return tool_error(f"Slack API error: {err}")


def _check_slack_post_thread() -> bool:
    """Available whenever a Slack token is resolvable."""
    return bool(_resolve_slack_token())


registry.register(
    name="slack_post_thread",
    toolset="slack_post",
    schema=SLACK_POST_THREAD_SCHEMA,
    handler=lambda args, **kw: _slack_post_thread_handler(args, **kw),
    check_fn=_check_slack_post_thread,
    requires_env=[],
    is_async=True,
    emoji="💬",
    max_result_size_chars=2000,
)
