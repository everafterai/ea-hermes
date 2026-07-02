import tools.approval as ap


def test_tool_requires_approval_matches_exact_and_glob(monkeypatch):
    monkeypatch.setattr(
        ap, "_get_approval_config",
        lambda: {"require_for_tools": ["stripe_api_write", "send_*"]},
    )
    assert ap.tool_requires_approval("stripe_api_write") is True
    assert ap.tool_requires_approval("send_message") is True
    assert ap.tool_requires_approval("SEND_MESSAGE") is True      # case-insensitive
    assert ap.tool_requires_approval("read_file") is False


def test_tool_requires_approval_inert_when_unset(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config", lambda: {})
    assert ap.tool_requires_approval("stripe_api_write") is False


def test_redact_tool_args_hides_secrets_and_truncates():
    from tools.approval import _redact_tool_args
    out = _redact_tool_args({
        "method": "POST",
        "path": "v1/refunds",
        "api_key": "sk_live_deadbeef",
        "authorization": "Bearer x",
        "body": "y" * 500,
    })
    # Every key is preserved — an approval prompt must not silently drop args.
    assert "method=POST" in out
    assert "path=v1/refunds" in out
    assert "api_key=<redacted>" in out and "sk_live_deadbeef" not in out
    assert "authorization=<redacted>" in out
    # The long body VALUE is truncated (not the full 500 chars); map kept whole.
    assert "body=" in out
    assert "y" * 500 not in out
    assert out.count("y") < 500  # value was significantly truncated


def test_redact_tool_args_empty():
    from tools.approval import _redact_tool_args
    assert _redact_tool_args({}) == "(no arguments)"
