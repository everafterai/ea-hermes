"""record_and_notify: audit event + best-effort owner DM."""
import json

import agent.automation_ownership as ao
import agent.data_access_audit as audit
from agent.automation_ownership import Identity
from hermes_constants import get_hermes_home

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _audit_lines():
    path = get_hermes_home() / "audit" / "data-access.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _record():
    return {"kind": "cron", "owner": {"platform": "slack", "user_id": "U_ALICE", "display_name": "Alice"},
            "collaborators": []}


def test_notify_writes_audit_event(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "notify_enabled", lambda: True)
    sent = {}
    monkeypatch.setattr(ao, "_send_dm", lambda platform, user_id, msg: sent.update(
        {"platform": platform, "user_id": user_id, "msg": msg}) or True)

    ao.record_and_notify("cron:j1", BOB, _record())

    lines = _audit_lines()
    assert len(lines) == 1
    assert lines[0]["action"] == "automation_edit"
    assert lines[0]["target"] == "cron:j1"
    assert sent["user_id"] == "U_ALICE" and "Bob" in sent["msg"]


def test_notify_disabled_skips_dm(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "notify_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(ao, "_send_dm", lambda *a, **k: calls.append(a) or True)
    ao.record_and_notify("cron:j1", BOB, _record())
    assert calls == []                       # no DM
    assert len(_audit_lines()) == 1          # audit still written


def test_notify_never_raises_on_dm_failure(monkeypatch):
    monkeypatch.setattr(audit, "_audit_config", lambda: {"enabled": True})
    monkeypatch.setattr(ao, "notify_enabled", lambda: True)
    def boom(*a, **k):
        raise RuntimeError("slack down")
    monkeypatch.setattr(ao, "_send_dm", boom)
    ao.record_and_notify("cron:j1", BOB, _record())   # must not raise
    assert len(_audit_lines()) == 1
