"""The file tool denies cross-user session/memory data and audits the attempt."""
import json

import agent.data_access_audit as audit
import tools.file_tools as ft
from hermes_constants import get_hermes_home


def _touch(p, text="secret-conversation"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _audit_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_read_file_denies_plaintext_session_snapshot(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    snap = _touch(get_hermes_home() / "sessions" / "session_abc.json")
    out = json.loads(ft.read_file_tool(str(snap)))
    assert "error" in out
    assert "security boundary" in out["error"].lower()
    assert any(ev["action"] == "blocked-read" for ev in _audit_lines())


def test_read_file_state_db_message_is_protected_not_terminal(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    db = _touch(get_hermes_home() / "state.db")
    out = json.loads(ft.read_file_tool(str(db)))
    assert "error" in out
    # Must NOT be the binary-guard message that points at the terminal bypass.
    assert "binary file" not in out["error"].lower()
    assert "other users" in out["error"].lower()


def test_search_filters_protected_snapshot(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    _touch(get_hermes_home() / "sessions" / "session_abc.json",
           "uniqueneedle12345 in another users chat")
    out = json.loads(ft.search_tool(
        pattern="uniqueneedle12345",
        path=str(get_hermes_home() / "sessions"),
        target="content",
    ))
    blob = json.dumps(out)
    assert "uniqueneedle12345" not in blob or out.get("matches") in (None, [])


def test_patch_denies_protected_db(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    db = _touch(get_hermes_home() / "state.db")
    out = json.loads(ft.patch_tool(
        mode="replace", path=str(db), old_string="x", new_string="y",
    ))
    assert "error" in out
    assert "security boundary" in out["error"].lower()


def test_global_memory_markdown_still_readable(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    md = _touch(get_hermes_home() / "memories" / "MEMORY.md", "shared note\n")
    out = json.loads(ft.read_file_tool(str(md)))
    assert "error" not in out
