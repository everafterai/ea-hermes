from gateway.session_context import set_session_vars, clear_session_vars
from tools.session_search_tool import resolve_search_scope


def test_scope_channel_for_shared_slack(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="slack", chat_type="group", chat_id="C123", user_id="U1")
    try:
        scope = resolve_search_scope()
    finally:
        clear_session_vars(tokens)
    assert scope == {"kind": "channel", "platform": "slack", "chat_id": "C123"}


def test_scope_user_for_dm(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="telegram", chat_type="dm", chat_id="D1", user_id="U9")
    try:
        scope = resolve_search_scope()
    finally:
        clear_session_vars(tokens)
    assert scope == {"kind": "user", "platform": "telegram", "user_id": "U9"}


def test_scope_admin_when_no_platform(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_TYPE", raising=False)
    assert resolve_search_scope() is None


def test_scope_fail_closed_when_identity_unresolvable(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="slack", chat_type="group", chat_id="", user_id="")
    try:
        scope = resolve_search_scope()
    finally:
        clear_session_vars(tokens)
    assert scope == {"kind": "none"}
