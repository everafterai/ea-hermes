"""cronjob enforces the cross-user ownership gate and registers creators."""
import json

import agent.automation_ownership as ao
import agent.data_access_audit as audit
import tools.cronjob_tools as cjt
from agent.automation_ownership import Identity

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _as(identity, monkeypatch):
    monkeypatch.setattr(ao, "current_identity", lambda: identity)
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: True)


def _create_as_alice(monkeypatch):
    _as(ALICE, monkeypatch)
    out = json.loads(cjt.cronjob(action="create", prompt="do a thing", schedule="30m", name="weekly"))
    # The created job id is at the top-level "job_id" key (not inside "job" which uses "job_id" too).
    # Verify the response shape for robustness: "job" dict uses key "job_id", top-level uses "job_id".
    return out


def test_create_registers_owner(monkeypatch):
    out = _create_as_alice(monkeypatch)
    # Top-level "job_id" is the canonical UUID; "job" sub-dict also has "job_id".
    job_id = out.get("job_id") or (out["job"]["job_id"] if "job" in out else None)
    assert ao.get_record(f"cron:{job_id}")["owner"]["user_id"] == "U_ALICE"


def test_cross_user_remove_blocked_without_confirm(monkeypatch):
    out = _create_as_alice(monkeypatch)
    job_id = out.get("job_id") or (out["job"]["job_id"] if "job" in out else None)
    _as(BOB, monkeypatch)
    res = json.loads(cjt.cronjob(action="remove", job_id=job_id))
    assert "error" in res and "owned by Alice" in res["error"]


def test_cross_user_remove_allowed_with_confirm(monkeypatch):
    out = _create_as_alice(monkeypatch)
    job_id = out.get("job_id") or (out["job"]["job_id"] if "job" in out else None)
    _as(BOB, monkeypatch)
    res = json.loads(cjt.cronjob(action="remove", job_id=job_id, confirm_cross_user_owner="Alice"))
    assert "error" not in res


def test_list_is_ungated(monkeypatch):
    _create_as_alice(monkeypatch)
    _as(BOB, monkeypatch)
    res = json.loads(cjt.cronjob(action="list"))
    assert "error" not in res
