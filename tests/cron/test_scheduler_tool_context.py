import cron.scheduler as sched
from cron.rbac_ceiling import cron_owner_grant  # noqa: F401 (patched via sched)


def test_scheduler_exports_owner_grant_and_acked(monkeypatch):
    captured = {}
    def fake_set(*, owner_grant, acked_tools, job_id=None):
        captured["grant"] = owner_grant
        captured["acked"] = acked_tools
        captured["job_id"] = job_id
        return "tok"
    monkeypatch.setattr(sched, "cron_owner_grant", lambda job: frozenset({"mcp-stripe"}))
    monkeypatch.setattr(sched, "set_cron_tool_context", fake_set)
    job = {"id": "j1", "unattended_approved_tools": ["stripe_api_write"]}
    tok = sched._enter_cron_tool_context(job)
    assert tok == "tok"
    assert captured["grant"] == frozenset({"mcp-stripe"})
    assert captured["acked"] == ["stripe_api_write"]
    assert captured["job_id"] == "j1"
