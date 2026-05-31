"""Integration tests for tool RBAC enforcement points."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.tool_access import denial_for_current_tool


class _FakePolicy:
    enabled = True

    def can_use_tool(self, user_id, toolset):
        return toolset == "web"


@pytest.fixture
def patched(monkeypatch):
    """Patch identity + policy + tool→toolset lookup used by the helper."""
    monkeypatch.setattr(
        "gateway.tool_access._current_identity",
        lambda: ("U_A", "slack"),
    )
    monkeypatch.setattr(
        "gateway.tool_access._policy_for_current_platform",
        lambda platform: _FakePolicy(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._toolset_for_tool",
        lambda name: {"web_search": "web", "run_shell": "terminal"}.get(name),
    )


def test_allows_permitted_tool(patched):
    assert denial_for_current_tool("web_search") is None


def test_denies_forbidden_tool(patched):
    msg = denial_for_current_tool("run_shell")
    assert msg is not None
    assert "run_shell" in msg


def test_no_identity_allows(monkeypatch):
    # CLI / system context: no user → no gating.
    monkeypatch.setattr(
        "gateway.tool_access._current_identity", lambda: (None, None)
    )
    assert denial_for_current_tool("run_shell") is None


def test_handle_function_call_blocks_forbidden_tool(monkeypatch):
    import model_tools

    monkeypatch.setattr(
        "gateway.tool_access._current_identity", lambda: ("U_A", "slack")
    )
    monkeypatch.setattr(
        "gateway.tool_access._policy_for_current_platform",
        lambda platform: _FakePolicy(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._toolset_for_tool",
        lambda name: {"run_shell": "terminal"}.get(name),
    )
    # Skip the plugin hook so only the RBAC backstop is exercised.
    out = model_tools.handle_function_call(
        "run_shell", {"command": "ls"}, skip_pre_tool_call_hook=True
    )
    assert "not permitted" in out


def test_disabled_policy_allows(monkeypatch):
    # When RBAC is disabled for the platform, the backstop must not gate.
    class _Disabled:
        enabled = False

        def can_use_tool(self, user_id, toolset):  # pragma: no cover
            raise AssertionError("can_use_tool should not be consulted when disabled")

    monkeypatch.setattr(
        "gateway.tool_access._current_identity", lambda: ("U_A", "slack")
    )
    monkeypatch.setattr(
        "gateway.tool_access._policy_for_current_platform",
        lambda platform: _Disabled(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._toolset_for_tool", lambda name: "terminal"
    )
    assert denial_for_current_tool("run_shell") is None


def test_unregistered_tool_allowed(monkeypatch):
    # toolset=None (tool not in registry) → fail open, not deny.
    monkeypatch.setattr(
        "gateway.tool_access._current_identity", lambda: ("U_A", "slack")
    )
    monkeypatch.setattr(
        "gateway.tool_access._policy_for_current_platform",
        lambda platform: _FakePolicy(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._toolset_for_tool", lambda name: None
    )
    assert denial_for_current_tool("some_unknown_tool") is None


from gateway.tool_access import filter_enabled_toolsets


class _RolePolicy:
    enabled = True

    def allowed_toolsets(self, user_id, all_toolsets):
        return frozenset({"web", "vision"}) & frozenset(all_toolsets)


def test_filter_intersects_with_role(monkeypatch):
    monkeypatch.setattr(
        "gateway.tool_access.policy_for_source",
        lambda cfg, src: _RolePolicy(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._load_config_cached", lambda: object(),
    )

    class _Src:
        user_id = "U_A"

    result = filter_enabled_toolsets(
        source=_Src(),
        enabled_toolsets=["web", "vision", "terminal", "file"],
    )
    assert sorted(result) == ["vision", "web"]


def test_filter_noop_when_disabled(monkeypatch):
    class _Disabled:
        enabled = False

    monkeypatch.setattr(
        "gateway.tool_access.policy_for_source",
        lambda cfg, src: _Disabled(),
    )
    monkeypatch.setattr(
        "gateway.tool_access._load_config_cached", lambda: object(),
    )

    class _Src:
        user_id = "U_A"

    result = filter_enabled_toolsets(
        source=_Src(),
        enabled_toolsets=["web", "terminal"],
    )
    assert sorted(result) == ["terminal", "web"]


class TestAuthGate:
    def _make_gateway(self):
        from gateway.run import GatewayRunner
        gw = GatewayRunner.__new__(GatewayRunner)
        # Stub instance attrs that the env-fallback path (disabled RBAC) touches.
        class _FakePairingStore:
            def is_approved(self, platform, user_id):
                return False
        gw.pairing_store = _FakePairingStore()
        return gw

    def _fake_policy(self, enabled, authorized):
        class _P:
            pass
        p = _P()
        p.enabled = enabled
        p.is_authorized = lambda uid: authorized
        return p

    def test_assigned_user_authorized(self, monkeypatch):
        from gateway.config import Platform
        from gateway.session import SessionSource

        gw = self._make_gateway()
        monkeypatch.setattr("gateway.tool_access._load_config_cached", lambda: object())
        monkeypatch.setattr(
            "gateway.tool_access.policy_for_source",
            lambda cfg, src: self._fake_policy(enabled=True, authorized=True),
        )
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_A")
        assert gw._is_user_authorized(src) is True

    def test_roleless_user_denied_overriding_env_and_allow_all(self, monkeypatch):
        from gateway.config import Platform
        from gateway.session import SessionSource

        gw = self._make_gateway()
        monkeypatch.setattr("gateway.tool_access._load_config_cached", lambda: object())
        monkeypatch.setattr(
            "gateway.tool_access.policy_for_source",
            lambda cfg, src: self._fake_policy(enabled=True, authorized=False),
        )
        # Even though the legacy env allowlist + allow-all would admit them:
        monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_STRANGER")
        monkeypatch.setenv("SLACK_ALLOW_ALL_USERS", "true")
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_STRANGER")
        assert gw._is_user_authorized(src) is False

    def test_rbac_disabled_falls_back_to_env(self, monkeypatch):
        from gateway.config import Platform
        from gateway.session import SessionSource

        gw = self._make_gateway()
        monkeypatch.setattr("gateway.tool_access._load_config_cached", lambda: object())
        monkeypatch.setattr(
            "gateway.tool_access.policy_for_source",
            lambda cfg, src: self._fake_policy(enabled=False, authorized=False),
        )
        monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_LEGACY")
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_LEGACY")
        # RBAC disabled → defers to env allowlist, which admits U_LEGACY.
        assert gw._is_user_authorized(src) is True


@pytest.mark.asyncio
async def test_rbac_active_unauthorized_dm_skips_pairing_offer(monkeypatch):
    """When RBAC is active for a platform and a roleless user sends a DM,
    the gateway must NOT offer a pairing code — the pairing_store.generate_code
    method must never be called, and the plain "ask an admin" message is sent."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    # Clear relevant auth env vars so legacy allowlist doesn't interfere.
    for key in ("SLACK_ALLOWED_USERS", "SLACK_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)

    # Patch RBAC to be active (enabled=True) but deny the user.
    class _ActivePolicy:
        enabled = True
        def is_authorized(self, uid):
            return False

    monkeypatch.setattr("gateway.tool_access._load_config_cached", lambda: object())
    monkeypatch.setattr(
        "gateway.tool_access.policy_for_source",
        lambda cfg, src: _ActivePolicy(),
    )

    config = GatewayConfig(
        platforms={Platform.SLACK: PlatformConfig(enabled=True, token="xoxb-test")},
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.SLACK: adapter}
    pairing_store = MagicMock()
    pairing_store.is_approved.return_value = False
    pairing_store._is_rate_limited.return_value = False
    runner.pairing_store = pairing_store

    event = MessageEvent(
        text="hello",
        message_id="m1",
        source=SessionSource(
            platform=Platform.SLACK,
            user_id="U_ROLELESS",
            chat_id="D_CHAN",
            user_name="roleless",
            chat_type="dm",
        ),
    )

    result = await runner._handle_message(event)

    assert result is None
    # Pairing code must NOT have been requested.
    pairing_store.generate_code.assert_not_called()
    # The plain "ask an admin" message must have been sent instead.
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert sent_text == "You're not authorized here. Ask an admin to assign you a role."
