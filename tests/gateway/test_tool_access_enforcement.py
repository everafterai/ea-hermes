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
