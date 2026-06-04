"""Tests for the terminal ``turn_end`` tool and its loop detection helper."""

from types import SimpleNamespace

from agent.conversation_loop import (
    _called_terminal_turn_end,
    _should_accept_silent_empty,
)


def _msg(*tool_names):
    """Build a fake assistant_message with the given tool-call names."""
    tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name=n)) for n in tool_names
    ]
    return SimpleNamespace(tool_calls=tool_calls)


def _reasoning_msg(**kw):
    """Fake assistant_message carrying structured reasoning fields."""
    base = {"reasoning": None, "reasoning_content": None, "reasoning_details": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_terminal_when_turn_end_called_and_flag_set():
    assert _called_terminal_turn_end(_msg("slack_react", "turn_end"), True) is True


def test_not_terminal_when_flag_unset():
    # Outside quiet channels turn_end is a no-op — never terminal.
    assert _called_terminal_turn_end(_msg("slack_react", "turn_end"), False) is False


def test_not_terminal_without_turn_end():
    assert _called_terminal_turn_end(_msg("slack_react"), True) is False


def test_not_terminal_with_no_tool_calls():
    assert _called_terminal_turn_end(SimpleNamespace(tool_calls=None), True) is False
    assert _called_terminal_turn_end(SimpleNamespace(tool_calls=[]), True) is False


def test_silent_empty_accepted_in_quiet_channel():
    # Empty response + quiet channel + no reasoning → silent finish.
    assert _should_accept_silent_empty(_reasoning_msg(), "", True) is True


def test_silent_empty_not_accepted_outside_quiet_channel():
    assert _should_accept_silent_empty(_reasoning_msg(), "", False) is False


def test_silent_empty_not_accepted_for_inline_thinking():
    # Model still mid-reasoning (visible <think>) → let prefill continue.
    assert _should_accept_silent_empty(_reasoning_msg(), "<think>...", True) is False


def test_silent_empty_not_accepted_for_structured_reasoning():
    msg = _reasoning_msg(reasoning_content="still thinking")
    assert _should_accept_silent_empty(msg, "", True) is False


def test_turn_end_tool_registered_in_slack_toolset():
    import tools.slack_react_tool  # noqa: F401  (register the tools)
    from tools.registry import registry
    entry = registry.get_entry("turn_end")
    assert entry is not None
    assert registry.get_toolset_for_tool("turn_end") == "slack"
    # Sync handler (no network) — must not be flagged async.
    assert entry.is_async is False


def test_turn_end_in_slack_toolset_and_default_bundle():
    import tools.slack_react_tool  # noqa: F401
    from toolsets import resolve_toolset
    assert "turn_end" in resolve_toolset("slack")
    # Must load on the Slack platform's default bundle, like slack_react.
    assert "turn_end" in resolve_toolset("hermes-slack")


def test_turn_end_handler_returns_ack():
    import json
    import tools.slack_react_tool as srt
    out = json.loads(srt._turn_end_handler({}))
    assert out.get("ok") is True
