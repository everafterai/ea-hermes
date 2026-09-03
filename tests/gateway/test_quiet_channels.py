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


def test_every_normalize_call_site_passes_quiet_completion_ok():
    """Every caller of ``_normalize_empty_agent_response`` must pass the flag.

    The flag defaults to False, so a call site that omits it silently emits
    "no response was generated" into a quiet channel and breaks the
    silent-listener contract. That is exactly what happened after the v0.20.6
    sync: upstream extracted the turn loop into ``TurnRunner`` and added a
    SECOND normalize call there, which ran before the gateway path that already
    carried the flag. Nothing failed — the bot just started talking in channels
    that are supposed to stay silent.

    Scanning the source (rather than asserting behaviour at one seam) is what
    catches the NEXT call site an upstream refactor introduces.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "gateway" / "run.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "_normalize_empty_agent_response":
            continue
        if not any(kw.arg == "quiet_completion_ok" for kw in node.keywords):
            missing.append(node.lineno)

    assert not missing, (
        "gateway/run.py calls _normalize_empty_agent_response without "
        f"quiet_completion_ok at line(s) {missing}. That call site will post "
        "'no response was generated' into quiet channels. Pass "
        "quiet_completion_ok=_is_quiet_channel(<source>, _load_gateway_config())."
    )
