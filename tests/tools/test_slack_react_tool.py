import json
import pytest

import tools.slack_react_tool as srt


@pytest.fixture(autouse=True)
def _fake_post(monkeypatch):
    calls = []

    async def fake_post(token, channel, ts, emoji, remove):
        calls.append({"token": token, "channel": channel, "ts": ts,
                      "emoji": emoji, "remove": remove})
        return {"ok": True}

    monkeypatch.setattr(srt, "_post_reaction", fake_post)
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "xoxb-test")
    return calls


def _run(args):
    from model_tools import _run_async
    return json.loads(_run_async(srt._slack_react_handler(args)))


def test_defaults_to_session_message_and_channel(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1",
        "HERMES_SESSION_MESSAGE_ID": "111.222",
    }.get(k, d))
    out = _run({"emoji": "party_sloth"})
    assert out["success"] is True
    assert _fake_post[0] == {"token": "xoxb-test", "channel": "C1",
                             "ts": "111.222", "emoji": "party_sloth", "remove": False}


def test_strips_colons_from_emoji(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    _run({"emoji": ":white_check_mark:"})
    assert _fake_post[0]["emoji"] == "white_check_mark"


def test_explicit_message_id_overrides_session(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    _run({"emoji": "eyes", "message_id": "9.9"})
    assert _fake_post[0]["ts"] == "9.9"


def test_remove_flag(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    _run({"emoji": "eyes", "remove": True})
    assert _fake_post[0]["remove"] is True


def test_error_when_no_emoji(monkeypatch, _fake_post):
    out = _run({})
    assert "error" in out


def test_error_when_no_target_channel(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": "")
    out = _run({"emoji": "eyes"})
    assert "error" in out


def test_error_when_no_token(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "")
    out = _run({"emoji": "eyes"})
    assert "error" in out


def test_already_reacted_is_treated_as_success(monkeypatch):
    async def fake(token, channel, ts, emoji, remove):
        return {"ok": False, "error": "already_reacted"}
    monkeypatch.setattr(srt, "_post_reaction", fake)
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "xoxb-test")
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    out = _run({"emoji": "eyes"})
    assert out.get("success") is True
    assert "error" not in out


def test_slack_api_error_surfaces(monkeypatch):
    async def fake(token, channel, ts, emoji, remove):
        return {"ok": False, "error": "channel_not_found"}
    monkeypatch.setattr(srt, "_post_reaction", fake)
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "xoxb-test")
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    out = _run({"emoji": "eyes"})
    assert "error" in out
    assert "channel_not_found" in out["error"]


def test_registered_in_registry():
    from tools.registry import registry
    import tools.slack_react_tool  # noqa: F401
    assert registry.get_entry("slack_react") is not None
    assert registry.get_toolset_for_tool("slack_react") == "slack"
    assert registry.get_entry("slack_react").is_async is True
