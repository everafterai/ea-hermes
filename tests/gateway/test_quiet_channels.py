from gateway.config import Platform
from gateway.session import SessionSource
from gateway.run import _parse_channel_id_list, _is_quiet_channel


def _src(chat_id="C1", parent=None, platform=Platform.SLACK):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="channel",
        parent_chat_id=parent,
    )


def test_parse_channel_id_list_splits_and_strips():
    assert _parse_channel_id_list("C1, C2 ,, C3") == {"C1", "C2", "C3"}


def test_parse_channel_id_list_handles_empty():
    assert _parse_channel_id_list("") == set()
    assert _parse_channel_id_list(None) == set()


def test_is_quiet_channel_matches_chat_id():
    cfg = {"slack": {"quiet_channels": "C1,C2"}}
    assert _is_quiet_channel(_src("C1"), cfg) is True
    assert _is_quiet_channel(_src("C9"), cfg) is False


def test_is_quiet_channel_matches_parent_for_threads():
    cfg = {"slack": {"quiet_channels": "C1"}}
    assert _is_quiet_channel(_src("T123", parent="C1"), cfg) is True


def test_is_quiet_channel_false_for_non_slack():
    cfg = {"slack": {"quiet_channels": "C1"}}
    assert _is_quiet_channel(_src("C1", platform=Platform.DISCORD), cfg) is False


def test_is_quiet_channel_false_when_unconfigured():
    assert _is_quiet_channel(_src("C1"), {}) is False
    assert _is_quiet_channel(_src("C1"), {"slack": {}}) is False


from gateway.run import _normalize_empty_agent_response


def test_normalize_suppresses_empty_success_when_quiet():
    result = {"api_calls": 2}  # did work, no error, no partial
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=True)
    assert out == ""


def test_normalize_keeps_empty_success_warning_when_not_quiet():
    result = {"api_calls": 2}
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=False)
    assert "no response was generated" in out


def test_normalize_surfaces_errors_even_when_quiet():
    result = {"failed": True, "error": "boom"}
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=True)
    assert "boom" in out


def test_normalize_surfaces_partial_even_when_quiet():
    result = {"api_calls": 1, "partial": True, "error": "stopped early"}
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=True)
    assert "stopped early" in out


def test_normalize_passes_through_real_text_when_quiet():
    out = _normalize_empty_agent_response({"api_calls": 1}, "hello", quiet_completion_ok=True)
    assert out == "hello"
