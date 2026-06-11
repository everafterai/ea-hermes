import json
import pytest

import tools.slack_react_tool as srt

# The autouse _fake_post fixture monkeypatches srt._resolve_slack_token, so
# capture the real implementation at import time for the resolver tests below.
_REAL_RESOLVE = srt._resolve_slack_token


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


# --- token resolution self-heal (child/delegated context without .env) -------

def test_resolve_token_reloads_dotenv_when_missing(monkeypatch):
    """A child/delegated process that never loaded ~/.hermes/.env starts with
    no SLACK_BOT_TOKEN in os.environ. _resolve_slack_token must load the
    dotenv and retry once rather than reporting the token as unconfigured."""
    import gateway.config
    import hermes_cli.env_loader

    # Gateway-config path yields nothing (no token in config.yaml).
    monkeypatch.setattr(
        gateway.config, "load_gateway_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no config")),
    )
    # Env starts empty — simulates the process never having loaded .env.
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    loaded = {"count": 0}

    def fake_load(**_kw):
        loaded["count"] += 1
        import os
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-from-dotenv"
        return []

    monkeypatch.setattr(hermes_cli.env_loader, "load_hermes_dotenv", fake_load)

    assert _REAL_RESOLVE() == "xoxb-from-dotenv"
    assert loaded["count"] == 1


def test_resolve_token_no_reload_when_already_present(monkeypatch):
    """When SLACK_BOT_TOKEN is already in env, the dotenv reload is skipped."""
    import gateway.config
    import hermes_cli.env_loader

    monkeypatch.setattr(
        gateway.config, "load_gateway_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no config")),
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-already-here")

    def boom(**_kw):
        raise AssertionError("load_hermes_dotenv should not be called")

    monkeypatch.setattr(hermes_cli.env_loader, "load_hermes_dotenv", boom)

    assert _REAL_RESOLVE() == "xoxb-already-here"


def test_resolve_token_empty_when_dotenv_has_no_token(monkeypatch):
    """If the dotenv reload still yields nothing, resolution returns '' (the
    caller surfaces a clean error) rather than raising."""
    import gateway.config
    import hermes_cli.env_loader

    monkeypatch.setattr(
        gateway.config, "load_gateway_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no config")),
    )
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(hermes_cli.env_loader, "load_hermes_dotenv", lambda **_k: [])

    assert _REAL_RESOLVE() == ""


# --- observability: the tool must log its failures (journal was silent) ------

def test_missing_token_is_logged(monkeypatch, caplog):
    """A missing token must emit a log record — the original incident left no
    trace in the gateway journal because the tool logged nothing."""
    import logging
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "")
    with caplog.at_level(logging.WARNING, logger="tools.slack_react_tool"):
        out = _run({"emoji": "eyes"})
    assert "error" in out
    assert any("token" in r.getMessage().lower() for r in caplog.records)


def test_slack_api_error_is_logged(monkeypatch, caplog):
    """A non-ok Slack API response must be logged with the error code."""
    import logging

    async def fake(token, channel, ts, emoji, remove):
        return {"ok": False, "error": "not_in_channel"}

    monkeypatch.setattr(srt, "_post_reaction", fake)
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "xoxb-test")
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    with caplog.at_level(logging.WARNING, logger="tools.slack_react_tool"):
        out = _run({"emoji": "eyes"})
    assert "not_in_channel" in out["error"]
    assert any("not_in_channel" in r.getMessage() for r in caplog.records)
