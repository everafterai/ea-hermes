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
