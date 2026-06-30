"""skill_manage enforces the cross-user ownership gate and registers creators."""
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import agent.automation_ownership as ao
import agent.data_access_audit as audit
import tools.skill_manager_tool as smt
from agent.automation_ownership import Identity

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")

_SKILL = """---
name: weekly-report
description: A weekly report skill for testing ownership gating behavior here.
---
Body.
"""

_SKILL_V2 = _SKILL.replace("Body.", "Body v2.")


@contextmanager
def _skill_dir(tmp_path):
    """Patch SKILLS_DIR and get_all_skills_dirs so _find_skill searches only tmp_path."""
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        yield


def _as(identity, monkeypatch):
    monkeypatch.setattr(ao, "current_identity", lambda: identity)
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: True)


def test_create_registers_owner(tmp_path, monkeypatch):
    _as(ALICE, monkeypatch)
    with _skill_dir(tmp_path):
        out = json.loads(smt.skill_manage(action="create", name="weekly-report", content=_SKILL))
    assert "error" not in out
    assert ao.get_record("skill:weekly-report")["owner"]["user_id"] == "U_ALICE"


def test_cross_user_edit_blocked_without_confirm(tmp_path, monkeypatch):
    with _skill_dir(tmp_path):
        _as(ALICE, monkeypatch)
        smt.skill_manage(action="create", name="weekly-report", content=_SKILL)
        _as(BOB, monkeypatch)
        out = json.loads(smt.skill_manage(action="edit", name="weekly-report", content=_SKILL_V2))
    assert "error" in out
    assert "owned by Alice" in out["error"]


def test_cross_user_edit_allowed_with_confirm(tmp_path, monkeypatch):
    with _skill_dir(tmp_path):
        _as(ALICE, monkeypatch)
        smt.skill_manage(action="create", name="weekly-report", content=_SKILL)
        _as(BOB, monkeypatch)
        out = json.loads(smt.skill_manage(
            action="edit", name="weekly-report", content=_SKILL_V2,
            confirm_cross_user_owner="Alice"))
    assert "error" not in out


def test_owner_edits_freely(tmp_path, monkeypatch):
    _as(ALICE, monkeypatch)
    with _skill_dir(tmp_path):
        smt.skill_manage(action="create", name="weekly-report", content=_SKILL)
        out = json.loads(smt.skill_manage(action="edit", name="weekly-report", content=_SKILL_V2))
    assert "error" not in out
