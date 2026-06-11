"""send_message token self-heal for child/delegated contexts.

The gateway loads ~/.hermes/.env into os.environ at startup, but a sub-agent
or worker process spawned for a turn may not have — leaving env-sourced
platform tokens (e.g. SLACK_BOT_TOKEN) absent, so load_gateway_config()
returns a tokenless/disabled platform. send_message must reload the dotenv
and re-read config once before reporting the platform unconfigured. Mirrors
the slack_react resolver fix.

Lives in its own file (not test_send_message_tool.py) so it runs in the bare
CI env — that module is skipped wholesale when python-telegram-bot is absent.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from tools.send_message_tool import send_message_tool


def _run_async_immediately(coro):
    return asyncio.run(coro)


def test_send_self_heals_when_dotenv_not_loaded():
    slack_platform = Platform("slack")
    tokenless = SimpleNamespace(enabled=False, token="", extra={})
    withtoken = SimpleNamespace(enabled=True, token="xoxb-real", extra={})
    cfg_before = SimpleNamespace(
        platforms={slack_platform: tokenless},
        get_home_channel=lambda _p: None,
    )
    cfg_after = SimpleNamespace(
        platforms={slack_platform: withtoken},
        get_home_channel=lambda _p: None,
    )
    configs = [cfg_before, cfg_after]

    def fake_load_config():
        return configs.pop(0) if len(configs) > 1 else configs[-1]

    reloads = {"n": 0}

    def fake_dotenv(**_kw):
        reloads["n"] += 1
        return []

    send = AsyncMock(return_value={"success": True})
    with patch("gateway.config.load_gateway_config", side_effect=fake_load_config), \
         patch("hermes_cli.env_loader.load_hermes_dotenv", side_effect=fake_dotenv), \
         patch("gateway.channel_directory.resolve_channel_name", return_value="C03B4BC9D2P"), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._send_to_platform", new=send), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        result = json.loads(
            send_message_tool({
                "action": "send",
                "target": "slack:C03B4BC9D2P",
                "message": "hi",
            })
        )

    assert reloads["n"] == 1
    assert result.get("success") is True
    # The token-bearing pconfig (from the reloaded config) must be what reached
    # the sender — not the tokenless first read.
    assert send.await_args.args[1] is withtoken


def test_send_no_reload_when_token_present():
    """A normally-configured platform must NOT trigger a dotenv reload."""
    slack_platform = Platform("slack")
    withtoken = SimpleNamespace(enabled=True, token="xoxb-real", extra={})
    config = SimpleNamespace(
        platforms={slack_platform: withtoken},
        get_home_channel=lambda _p: None,
    )

    def boom(**_kw):
        raise AssertionError("load_hermes_dotenv should not be called")

    send = AsyncMock(return_value={"success": True})
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("hermes_cli.env_loader.load_hermes_dotenv", side_effect=boom), \
         patch("gateway.channel_directory.resolve_channel_name", return_value="C03B4BC9D2P"), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._send_to_platform", new=send), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        result = json.loads(
            send_message_tool({
                "action": "send",
                "target": "slack:C03B4BC9D2P",
                "message": "hi",
            })
        )

    assert result.get("success") is True
