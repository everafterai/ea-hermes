import cron.scheduler as sched


def test_ceiling_caps_resolved(monkeypatch):
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: ["terminal", "web"])
    monkeypatch.setattr("cron.rbac_ceiling.cron_owner_grant", lambda job: frozenset({"web"}))
    out = sched._cron_enabled_toolsets_with_ceiling({"id": "abc"}, {})
    assert "terminal" not in out
    assert "web" in out


def test_ceiling_noop_when_ownerless(monkeypatch):
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: ["terminal"])
    monkeypatch.setattr("cron.rbac_ceiling.cron_owner_grant", lambda job: None)
    out = sched._cron_enabled_toolsets_with_ceiling({"id": "abc"}, {})
    assert out == ["terminal"]
