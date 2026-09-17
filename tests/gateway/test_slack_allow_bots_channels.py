"""``slack.allow_bots_channels`` — admit app/bot-authored messages in a short
list of channels without flipping the workspace-wide ``allow_bots`` switch.

Two layers must agree. The adapter's inbound filter decides whether an
app post is even *processed*; the gateway's ``_is_user_authorized`` decides
whether the sender may talk to the agent. An app post arrives with
``user=None``, so without a channel admission it dies at the no-user-id guard
before RBAC's ``channel_roles`` (the fork's mechanism for "every poster in this
channel") ever sees it. And under RBAC the legacy ``SLACK_ALLOW_BOTS`` bypass
must not outrank roles — RBAC is the sole authorization source.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()
import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True
from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402

LEADS = "C0LEADS"
OTHER = "C0OTHER"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("SLACK_ALLOW_BOTS", "SLACK_ALLOW_BOTS_CHANNELS", "SLACK_ALLOWED_USERS",
                "SLACK_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(var, raising=False)


def _adapter(**extra):
    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._bot_user_id = "U_BOT"
    adapter._team_bot_user_ids = {}
    return adapter


# ─── adapter: parsing ────────────────────────────────────────────────────────


def test_channel_list_from_config_string():
    assert _adapter(allow_bots_channels=f"{LEADS}, {OTHER}")._slack_allow_bots_channels() == {LEADS, OTHER}


def test_channel_list_from_config_list():
    assert _adapter(allow_bots_channels=[LEADS])._slack_allow_bots_channels() == {LEADS}


def test_channel_list_env_fallback(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOW_BOTS_CHANNELS", LEADS)
    assert _adapter()._slack_allow_bots_channels() == {LEADS}


def test_channel_list_empty_by_default():
    assert _adapter()._slack_allow_bots_channels() == set()


# ─── adapter: policy resolution ──────────────────────────────────────────────


def test_listed_channel_is_all_even_when_global_is_none():
    adapter = _adapter(allow_bots_channels=LEADS)
    assert adapter._slack_allow_bots(LEADS) == "all"
    assert adapter._slack_allow_bots(OTHER) == "none"
    assert adapter._slack_allow_bots() == "none"


def test_listed_channel_does_not_loosen_a_global_mentions_policy_elsewhere():
    adapter = _adapter(allow_bots=" mentions", allow_bots_channels=LEADS)
    assert adapter._slack_allow_bots(LEADS) == "all"
    assert adapter._slack_allow_bots(OTHER) == "mentions"


def test_global_all_still_applies_everywhere():
    adapter = _adapter(allow_bots="all")
    assert adapter._slack_allow_bots(OTHER) == "all"


# ─── adapter: yaml → env bridge ──────────────────────────────────────────────


def test_apply_yaml_config_bridges_channel_list(monkeypatch):
    import os
    monkeypatch.delenv("SLACK_ALLOW_BOTS_CHANNELS", raising=False)
    _slack_mod._apply_yaml_config({}, {"allow_bots_channels": [LEADS, OTHER]})
    assert os.environ.get("SLACK_ALLOW_BOTS_CHANNELS") == f"{LEADS},{OTHER}"


def test_apply_yaml_config_env_wins_over_yaml(monkeypatch):
    import os
    monkeypatch.setenv("SLACK_ALLOW_BOTS_CHANNELS", OTHER)
    _slack_mod._apply_yaml_config({}, {"allow_bots_channels": LEADS})
    assert os.environ["SLACK_ALLOW_BOTS_CHANNELS"] == OTHER


# ─── gateway: authorization gate ─────────────────────────────────────────────


def _runner(adapter=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    runner._adapter_for_source = lambda source: adapter
    return runner


def _app_post(chat_id):
    return SessionSource(platform=Platform.SLACK, chat_id=chat_id, chat_type="group",
                         user_id=None, user_name="", is_bot=True)


@pytest.fixture
def no_rbac(monkeypatch):
    from gateway import tool_access
    monkeypatch.setattr(tool_access, "_load_config_cached",
                        lambda: SimpleNamespace(platforms={Platform.SLACK: PlatformConfig(enabled=True, extra={})}))


@pytest.fixture
def rbac(monkeypatch):
    """RBAC on: one human role, and the leads channel carries a service role."""
    from gateway import tool_access
    cfg = SimpleNamespace(platforms={Platform.SLACK: PlatformConfig(enabled=True, extra={
        "user_roles": {"U1": "admin"},
        "roles": {"lead_prep": {"toolsets": ["web"]}},
        "channel_roles": {LEADS: "lead_prep"},
    })})
    monkeypatch.setattr(tool_access, "_load_config_cached", lambda: cfg)


def test_app_post_in_listed_channel_is_authorized(no_rbac):
    runner = _runner(_adapter(allow_bots_channels=LEADS))
    assert runner._is_user_authorized(_app_post(LEADS)) is True


def test_app_post_in_unlisted_channel_is_rejected_under_global_none(no_rbac):
    runner = _runner(_adapter(allow_bots_channels=LEADS))
    assert runner._is_user_authorized(_app_post(OTHER)) is False


def test_rbac_listed_channel_with_channel_role_is_authorized(rbac):
    runner = _runner(_adapter(allow_bots_channels=LEADS))
    assert runner._is_user_authorized(_app_post(LEADS)) is True


def test_rbac_listed_channel_without_channel_role_is_rejected(rbac, monkeypatch):
    """Admission at the adapter is not authorization: under RBAC the channel
    must also carry a channel_roles entry, or the app has no grant."""
    runner = _runner(_adapter(allow_bots_channels=f"{LEADS},{OTHER}"))
    assert runner._is_user_authorized(_app_post(OTHER)) is False


def test_rbac_outranks_the_global_allow_bots_bypass(rbac, monkeypatch):
    """SLACK_ALLOW_BOTS=all used to authorize any app anywhere before RBAC ran.
    RBAC is the sole authorization source: no channel role, no entry."""
    monkeypatch.setenv("SLACK_ALLOW_BOTS", "all")
    runner = _runner(_adapter(allow_bots="all"))
    assert runner._is_user_authorized(_app_post(OTHER)) is False
    assert runner._is_user_authorized(_app_post(LEADS)) is True


def test_legacy_global_bypass_unchanged_without_rbac(no_rbac, monkeypatch):
    monkeypatch.setenv("SLACK_ALLOW_BOTS", "all")
    runner = _runner(_adapter(allow_bots="all"))
    assert runner._is_user_authorized(_app_post(OTHER)) is True
