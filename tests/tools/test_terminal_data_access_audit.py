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
