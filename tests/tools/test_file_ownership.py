"""The file tool gates cross-user edits of managed automation files."""
import json

import agent.automation_ownership as ao
import agent.data_access_audit as audit
import tools.file_tools as ft
from agent.automation_ownership import Identity
from hermes_constants import get_hermes_home

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _as(identity, monkeypatch):
    monkeypatch.setattr(ao, "current_identity", lambda: identity)
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: True)


def test_new_script_registers_creator(monkeypatch):
    _as(ALICE, monkeypatch)
    p = get_hermes_home() / "scripts" / "foo.sh"
    out = json.loads(ft.write_file_tool(str(p), "echo hi\n"))
    assert "error" not in out
    assert ao.get_record("script:foo.sh")["owner"]["user_id"] == "U_ALICE"


def test_cross_user_script_patch_blocked(monkeypatch):
    _as(ALICE, monkeypatch)
    p = get_hermes_home() / "scripts" / "foo.sh"
    ft.write_file_tool(str(p), "echo hi\n")
    _as(BOB, monkeypatch)
    out = json.loads(ft.patch_tool(mode="replace", path=str(p),
                                   old_string="echo hi", new_string="echo bye"))
    assert "error" in out and "owned by Alice" in out["error"]


def test_cross_user_script_patch_allowed_with_confirm(monkeypatch):
    _as(ALICE, monkeypatch)
    p = get_hermes_home() / "scripts" / "foo.sh"
    ft.write_file_tool(str(p), "echo hi\n")
    _as(BOB, monkeypatch)
    out = json.loads(ft.patch_tool(mode="replace", path=str(p),
                                   old_string="echo hi", new_string="echo bye",
                                   confirm_cross_user_owner="Alice"))
    assert "error" not in out


def test_unrelated_file_is_untouched(monkeypatch):
    _as(BOB, monkeypatch)
    p = get_hermes_home() / "workspace" / "notes.txt"
    out = json.loads(ft.write_file_tool(str(p), "hello\n"))
    assert "error" not in out  # not a managed automation path -> no gate


def test_unowned_script_patch_surfaces_notice(monkeypatch):
    """Script on disk with no ownership record: patch succeeds with ownership_notice."""
    p = get_hermes_home() / "scripts" / "foo.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("echo hi\n", encoding="utf-8")
    # No ownership record registered (written directly, not via tool).
    assert ao.get_record("script:foo.sh") is None
    _as(BOB, monkeypatch)
    out = json.loads(ft.patch_tool(mode="replace", path=str(p),
                                   old_string="echo hi", new_string="echo bye"))
    assert "error" not in out
    assert "ownership_notice" in out
    assert "claim" in out["ownership_notice"].lower()
