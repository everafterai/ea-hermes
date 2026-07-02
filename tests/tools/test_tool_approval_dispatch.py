import json
import model_tools
import tools.approval as ap


def test_dispatch_blocks_gated_tool_on_deny(monkeypatch):
    monkeypatch.setattr(ap, "check_tool_approval",
                        lambda name, args, sk: {"approved": False, "message": "BLOCKED: nope"})
    # read_file is a real, always-registered tool; the gate must fire before it runs.
    out = model_tools.handle_function_call("read_file", {"file_path": "/etc/hostname"})
    data = json.loads(out)
    assert data.get("status") == "blocked"
    assert "BLOCKED" in data.get("error", "")


def test_dispatch_allows_when_gate_approves(monkeypatch):
    seen = {"called": False}
    def fake_gate(name, args, sk):
        seen["called"] = True
        return {"approved": True, "message": None}
    monkeypatch.setattr(ap, "check_tool_approval", fake_gate)
    model_tools.handle_function_call("read_file", {"file_path": "/etc/hostname"})
    assert seen["called"] is True


def test_dispatch_gate_error_fails_closed_in_cron(monkeypatch):
    import json, model_tools
    import tools.approval as ap
    monkeypatch.setattr(ap, "check_tool_approval",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    out = model_tools.handle_function_call("read_file", {"file_path": "/etc/hostname"})
    assert json.loads(out).get("status") == "blocked"


def test_dispatch_gate_error_fails_open_when_interactive(monkeypatch):
    import json, model_tools
    import tools.approval as ap
    monkeypatch.setattr(ap, "check_tool_approval",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    out = model_tools.handle_function_call("read_file", {"file_path": "/etc/hostname"})
    # Interactive: the gate error must NOT block a permitted tool.
    assert json.loads(out).get("status") != "blocked"
