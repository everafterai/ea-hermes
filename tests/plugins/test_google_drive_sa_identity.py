"""Tests for plugins/google_drive_sa/identity.py — platform user id → email."""

from __future__ import annotations

import pytest

from plugins.google_drive_sa import identity


@pytest.fixture(autouse=True)
def _reset():
    identity.reset_cache()
    yield
    identity.reset_cache()


def test_config_map_wins_and_lowercases(monkeypatch):
    monkeypatch.setattr(
        identity, "_raw_config",
        lambda: {"slack": {"user_emails": {"U1": "Alice@EverAfter.ai"}}},
    )
    calls = []
    monkeypatch.setattr(identity, "_slack_users_info", lambda uid: calls.append(uid) or {})
    assert identity.resolve_email("slack", "U1") == "alice@everafter.ai"
    assert calls == []


def test_config_map_numeric_ids_are_stringified(monkeypatch):
    monkeypatch.setattr(
        identity, "_raw_config",
        lambda: {"slack": {"user_emails": {123: "a@x.io"}}},
    )
    assert identity.resolve_email("slack", "123") == "a@x.io"


def test_slack_lookup_then_persist(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})
    monkeypatch.setattr(
        identity, "_slack_users_info",
        lambda uid: {"ok": True, "user": {"id": uid, "profile": {"email": "Bob@EverAfter.ai"}}},
    )
    persisted = []
    monkeypatch.setattr(identity, "_persist_email", lambda p, u, e: persisted.append((p, u, e)))
    assert identity.resolve_email("slack", "U2") == "bob@everafter.ai"
    assert persisted == [("slack", "U2", "bob@everafter.ai")]


def test_slack_lookup_result_cached_in_memory_even_if_persist_fails(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})
    calls = []

    def _info(uid):
        calls.append(uid)
        return {"ok": True, "user": {"profile": {"email": "c@x.io"}}}

    monkeypatch.setattr(identity, "_slack_users_info", _info)

    def _boom(p, u, e):
        raise OSError("read-only fs")

    monkeypatch.setattr(identity, "_persist_email", _boom)
    assert identity.resolve_email("slack", "U3") == "c@x.io"
    assert identity.resolve_email("slack", "U3") == "c@x.io"
    assert calls == ["U3"]  # second call hit neither Slack nor disk


def test_slack_error_response_returns_none(monkeypatch, caplog):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})
    monkeypatch.setattr(identity, "_slack_users_info", lambda uid: {"ok": False, "error": "missing_scope"})
    persisted = []
    monkeypatch.setattr(identity, "_persist_email", lambda p, u, e: persisted.append(e))
    with caplog.at_level("WARNING"):
        assert identity.resolve_email("slack", "U4") is None
    assert persisted == []
    assert "users:read.email" in caplog.text


def test_slack_exception_returns_none(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})

    def _boom(uid):
        raise RuntimeError("network")

    monkeypatch.setattr(identity, "_slack_users_info", _boom)
    assert identity.resolve_email("slack", "U5") is None


def test_non_slack_platform_uses_map_but_no_api_fallback(monkeypatch):
    monkeypatch.setattr(
        identity, "_raw_config",
        lambda: {"google_chat": {"user_emails": {"users/9": "g@x.io"}}},
    )
    calls = []
    monkeypatch.setattr(identity, "_slack_users_info", lambda uid: calls.append(uid) or {})
    assert identity.resolve_email("google_chat", "users/9") == "g@x.io"
    assert identity.resolve_email("google_chat", "users/10") is None
    assert calls == []


def test_empty_identity_returns_none(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {})
    assert identity.resolve_email("", "U1") is None
    assert identity.resolve_email("slack", "") is None


def test_persist_email_writes_config_via_users_helper(monkeypatch, tmp_path):
    """Real write-back path: ruamel round-trip through hermes_cli.users._mutate_slack."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        "# keep me\n"
        "slack:\n"
        "  user_roles:\n"
        "    U1: operator  # alice\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    identity._persist_email("slack", "U1", "alice@everafter.ai")
    text = cfg.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "# alice" in text
    assert "user_emails:" in text and "U1: alice@everafter.ai" in text


def test_persist_email_non_slack_is_noop(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg = hermes_home / "config.yaml"
    cfg.write_text("slack:\n  user_roles: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    identity._persist_email("google_chat", "users/1", "g@x.io")
    assert "user_emails" not in cfg.read_text(encoding="utf-8")
