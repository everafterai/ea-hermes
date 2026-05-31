"""Integration tests for tool RBAC enforcement points."""
from __future__ import annotations

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
