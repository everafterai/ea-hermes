"""Tests for the local append-only data-access audit log."""
import json

import agent.data_access_audit as audit
from hermes_constants import get_hermes_home


def _read_log_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_record_access_writes_one_jsonl_line(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_access(tool="read_file", action="blocked-read", target="/x/state.db")
    lines = _read_log_lines()
    assert len(lines) == 1
    ev = lines[0]
    assert ev["tool"] == "read_file"
    assert ev["action"] == "blocked-read"
    assert ev["target"] == "/x/state.db"
    assert "ts" in ev
    # Identity keys are always present (empty when no session context).
    for k in ("platform", "user_id", "chat_id", "session_id"):
        assert k in ev


def test_record_access_appends(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_access(tool="read_file", action="blocked-read", target="/a")
    audit.record_access(tool="patch", action="blocked-read", target="/b")
    assert len(_read_log_lines()) == 2


def test_disabled_writes_nothing(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": False})
    audit.record_access(tool="read_file", action="blocked-read", target="/x")
    assert _read_log_lines() == []


def test_record_command_access_logs_on_marker(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_command_access("sqlite3 ~/.hermes/state.db .dump", tool="terminal")
    lines = _read_log_lines()
    assert len(lines) == 1
    assert lines[0]["action"] == "exec"
    assert lines[0]["tool"] == "terminal"


def test_record_command_access_ignores_unrelated(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    audit.record_command_access("ls -la /tmp && echo done", tool="terminal")
    assert _read_log_lines() == []


def test_audit_never_raises(monkeypatch):
    # A broken config must not propagate into the tool path.
    def boom():
        raise RuntimeError("config exploded")
    monkeypatch.setattr(audit, "_audit_config", boom)
    # Should swallow and return None, not raise.
    audit.record_access(tool="read_file", action="blocked-read", target="/x")
    audit.record_command_access("sqlite3 state.db", tool="terminal")
