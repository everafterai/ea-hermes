"""terminal / code_execution emit an exec audit event when a command
references a protected data store."""
import json

import agent.data_access_audit as audit
from hermes_constants import get_hermes_home


def _audit_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_terminal_handler_imports_scanner():
    # The call site uses a local import; assert the symbol is importable
    # the same way the handler imports it.
    from agent.data_access_audit import record_command_access  # noqa: F401


def test_scan_logs_terminal_reference(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_command_access("strings ~/.hermes/state.db | grep secret", tool="terminal")
    lines = _audit_lines()
    assert len(lines) == 1
    assert lines[0]["tool"] == "terminal"
    assert lines[0]["action"] == "exec"


def test_scan_logs_code_execution_reference(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    code = "open('/root/.hermes/sessions/session_x.json').read()"
    audit.record_command_access(code, tool="code_execution")
    lines = _audit_lines()
    assert len(lines) == 1
    assert lines[0]["tool"] == "code_execution"


def test_terminal_handler_invokes_scanner(monkeypatch):
    import tools.terminal_tool as tt
    from unittest.mock import MagicMock
    spy = MagicMock()
    monkeypatch.setattr("agent.data_access_audit.record_command_access", spy)
    def _boom():
        raise RuntimeError("stop-after-audit")
    monkeypatch.setattr(tt, "_get_env_config", _boom)
    # Call terminal_tool directly — it contains the record_command_access call.
    # The outer try/except catches the RuntimeError from _get_env_config and
    # returns an error JSON, but the scanner has already been invoked.
    tt.terminal_tool(command="sqlite3 ~/.hermes/state.db .dump")
    assert spy.called
    assert spy.call_args.kwargs.get("tool") == "terminal"


def test_execute_code_handler_invokes_scanner(monkeypatch):
    import tools.code_execution_tool as cet
    from unittest.mock import MagicMock
    spy = MagicMock()
    monkeypatch.setattr("agent.data_access_audit.record_command_access", spy)
    monkeypatch.setattr(cet, "SANDBOX_AVAILABLE", True)
    # Deny at the approval guard (runs AFTER the audit call) so no sandbox spawns.
    monkeypatch.setattr("tools.approval.check_execute_code_guard",
                        lambda code, env_type: {"approved": False, "message": "blocked-for-test"})
    cet.execute_code(code="cat ~/.hermes/state.db")
    assert spy.called
    assert spy.call_args.kwargs.get("tool") == "code_execution"
