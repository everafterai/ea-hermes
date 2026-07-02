import threading

import tools.approval as ap
from cron.tool_approval_context import set_cron_tool_context, clear_cron_tool_context


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


def _gate_on(monkeypatch, tools_list=("gated_tool",)):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": list(tools_list)})


def test_check_tool_approval_not_gated(monkeypatch):
    _gate_on(monkeypatch)
    assert ap.check_tool_approval("read_file", {}, "s1") == {"approved": True, "message": None}


def test_check_tool_approval_cli_session_grants_and_sticks(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    calls = {"n": 0}

    def fake_prompt(command, description, timeout_seconds=None, allow_permanent=True, approval_callback=None):
        calls["n"] += 1
        assert allow_permanent is False          # gated tools never permanent
        return "session"

    monkeypatch.setattr(ap, "prompt_dangerous_approval", fake_prompt)
    first = ap.check_tool_approval("gated_tool", {"path": "v1/refunds"}, "s2")
    assert first == {"approved": True, "message": None}
    second = ap.check_tool_approval("gated_tool", {"path": "v1/refunds"}, "s2")
    assert second == {"approved": True, "message": None}
    assert calls["n"] == 1                         # session approval reused, no re-prompt


def test_check_tool_approval_cli_deny_blocks(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "prompt_dangerous_approval",
                        lambda *a, **k: "deny")
    res = ap.check_tool_approval("gated_tool", {}, "s3")
    assert res["approved"] is False and "BLOCKED" in res["message"]


def test_check_tool_approval_gateway_blocks_via_helper(monkeypatch):
    _gate_on(monkeypatch)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(ap, "_await_tool_gateway_decision",
                        lambda sk, tool, desc, timeout_seconds: "once")
    res = ap.check_tool_approval("gated_tool", {}, "s4")
    assert res == {"approved": True, "message": None}


def test_cron_skips_when_acked_and_owner_has_grant(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"]})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "_toolset_for_tool", lambda name: "mcp-stripe")
    tok = set_cron_tool_context(owner_grant=frozenset({"mcp-stripe"}),
                                acked_tools=["stripe_api_write"])
    try:
        res = ap.check_tool_approval("stripe_api_write", {"method": "POST"}, "cronsess")
        assert res == {"approved": True, "message": None}
    finally:
        clear_cron_tool_context(tok)


def test_cron_denies_when_not_acked(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"], "cron_mode": "deny"})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "_toolset_for_tool", lambda name: "mcp-stripe")
    tok = set_cron_tool_context(owner_grant=frozenset({"mcp-stripe"}), acked_tools=[])
    try:
        res = ap.check_tool_approval("stripe_api_write", {}, "cronsess")
        assert res["approved"] is False
    finally:
        clear_cron_tool_context(tok)


def test_cron_denies_when_owner_lacks_grant(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"], "cron_mode": "deny"})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(ap, "_toolset_for_tool", lambda name: "mcp-stripe")
    tok = set_cron_tool_context(owner_grant=frozenset({"web"}), acked_tools=["stripe_api_write"])
    try:
        assert ap.check_tool_approval("stripe_api_write", {}, "cronsess")["approved"] is False
    finally:
        clear_cron_tool_context(tok)


def test_cron_fails_closed_on_internal_error(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"], "cron_mode": "deny"})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    # _toolset_for_tool raising must NOT approve — it must deny (fail closed).
    monkeypatch.setattr(ap, "_toolset_for_tool",
                        lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    tok = set_cron_tool_context(owner_grant=frozenset({"mcp-stripe"}), acked_tools=["stripe_api_write"])
    try:
        res = ap.check_tool_approval("stripe_api_write", {}, "cronsess")
        assert res["approved"] is False
    finally:
        clear_cron_tool_context(tok)


def test_headless_outer_exception_fails_closed(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config",
                        lambda: {"require_for_tools": ["stripe_api_write"]})
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: False)
    # Make an early step raise so the OUTER except is exercised in a headless context.
    monkeypatch.setattr(ap, "_redact_tool_args",
                        lambda args, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = ap.check_tool_approval("stripe_api_write", {"x": 1}, "cronsess2")
    assert res["approved"] is False
