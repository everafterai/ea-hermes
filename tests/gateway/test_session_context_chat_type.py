from gateway.session_context import (
    set_session_vars,
    clear_session_vars,
    get_session_env,
)


def test_chat_type_roundtrips():
    tokens = set_session_vars(platform="slack", chat_id="C1", chat_type="group", user_id="U1")
    try:
        assert get_session_env("HERMES_SESSION_CHAT_TYPE") == "group"
    finally:
        clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_CHAT_TYPE") == ""
