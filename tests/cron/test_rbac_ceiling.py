from cron.rbac_ceiling import apply_cron_toolset_ceiling


def test_no_grant_returns_resolved_unchanged():
    assert apply_cron_toolset_ceiling(["terminal", "web"], None) == ["terminal", "web"]
    assert apply_cron_toolset_ceiling(None, None) is None


def test_wildcard_grant_returns_resolved_unchanged():
    assert apply_cron_toolset_ceiling(["terminal"], frozenset({"*"})) == ["terminal"]
    assert apply_cron_toolset_ceiling(None, frozenset({"*"})) is None


def test_caps_to_grant_and_keeps_floor():
    grant = frozenset({"web", "vision"})
    out = apply_cron_toolset_ceiling(["terminal", "web", "todo"], grant)
    assert "terminal" not in out
    assert "web" in out
    assert "todo" in out  # 'todo' is a FLOOR toolset, always kept


def test_chat_only_grant_caps_to_floor_only():
    out = apply_cron_toolset_ceiling(["terminal", "web", "todo"], frozenset())
    assert out == ["todo"]


def test_unset_resolved_expands_then_caps(monkeypatch):
    monkeypatch.setattr("toolsets.get_all_toolsets", lambda: ["terminal", "web", "file"])
    out = apply_cron_toolset_ceiling(None, frozenset({"web"}))
    assert out == ["web"]


import cron.rbac_ceiling as ceiling


class _FakePolicy:
    enabled = True

    def __init__(self, grant):
        self._grant = grant

    def grant_for(self, user_id, chat_id=None):
        return self._grant


def _record(user_id="U1", platform="slack"):
    return {"owner": {"user_id": user_id, "platform": platform, "display_name": "Bob"}}


def test_owner_grant_none_when_ownership_disabled(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: False)
    assert ceiling.cron_owner_grant({"id": "abc"}) is None


def test_owner_grant_none_when_no_record(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: True)
    monkeypatch.setattr("agent.automation_ownership.get_record", lambda key: None)
    assert ceiling.cron_owner_grant({"id": "abc"}) is None


def test_owner_grant_none_when_rbac_inactive(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: True)
    monkeypatch.setattr("agent.automation_ownership.get_record", lambda key: _record())

    class _Disabled:
        enabled = False

    monkeypatch.setattr("gateway.tool_access.policy_for_platform", lambda name: _Disabled())
    assert ceiling.cron_owner_grant({"id": "abc"}) is None


def test_owner_grant_resolves_role(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.is_enabled", lambda: True)
    monkeypatch.setattr("agent.automation_ownership.get_record", lambda key: _record())
    monkeypatch.setattr(
        "gateway.tool_access.policy_for_platform",
        lambda name: _FakePolicy(frozenset({"web"})),
    )
    assert ceiling.cron_owner_grant({"id": "abc"}) == frozenset({"web"})
