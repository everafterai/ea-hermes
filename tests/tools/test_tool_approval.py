import threading

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


def test_await_tool_gateway_decision_blocks_then_resolves():
    sk = "sess-xyz"
    seen = {}
    ap.register_gateway_notify(sk, lambda data: seen.update(data))
    try:
        # Resolve from another thread shortly after the wait begins.
        t = threading.Timer(0.2, lambda: ap.resolve_gateway_approval(sk, "session"))
        t.start()
        choice = ap._await_tool_gateway_decision(sk, "stripe_api_write", "money movement", timeout_seconds=5)
        assert choice == "session"
        assert seen.get("tool_name") == "stripe_api_write"
        assert seen.get("description") == "money movement"
    finally:
        ap.unregister_gateway_notify(sk)


def test_await_tool_gateway_decision_times_out_to_deny():
    sk = "sess-timeout"
    ap.register_gateway_notify(sk, lambda data: None)
    try:
        assert ap._await_tool_gateway_decision(sk, "t", "d", timeout_seconds=0.1) == "deny"
    finally:
        ap.unregister_gateway_notify(sk)
