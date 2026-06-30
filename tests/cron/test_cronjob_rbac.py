import tools.cronjob_tools as cj
from agent.automation_ownership import Identity


class _Policy:
    enabled = True

    def __init__(self, grant):
        self._grant = grant

    def allowed_toolsets(self, user_id, requested, chat_id=None):
        return frozenset(t for t in requested if "*" in self._grant or t in self._grant)

    def can_use_tool(self, user_id, toolset, chat_id=None):
        return "*" in self._grant or toolset in self._grant


def _setup(monkeypatch, grant, identity=Identity("slack", "U1", "Bob")):
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda *a, **k: "")
    monkeypatch.setattr("agent.automation_ownership.current_identity", lambda: identity)
    monkeypatch.setattr("gateway.tool_access.policy_for_platform", lambda name: _Policy(grant))


def test_rejects_overprivileged_toolset(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    err = cj._rbac_creation_error(enabled_toolsets=["terminal"], has_script=False, is_no_agent=False)
    assert err and "terminal" in err


def test_allows_within_role(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    assert cj._rbac_creation_error(enabled_toolsets=["web"], has_script=False, is_no_agent=False) is None


def test_script_requires_shell_role(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    err = cj._rbac_creation_error(enabled_toolsets=None, has_script=True, is_no_agent=False)
    assert err and ("terminal" in err or "code_execution" in err)


def test_no_agent_requires_shell_role(monkeypatch):
    _setup(monkeypatch, frozenset({"web"}))
    err = cj._rbac_creation_error(enabled_toolsets=None, has_script=False, is_no_agent=True)
    assert err and ("terminal" in err or "code_execution" in err)


def test_admin_allowed(monkeypatch):
    _setup(monkeypatch, frozenset({"*"}))
    assert cj._rbac_creation_error(enabled_toolsets=["terminal"], has_script=True, is_no_agent=True) is None


def test_no_identity_is_trusted(monkeypatch):
    monkeypatch.setattr("agent.automation_ownership.current_identity", lambda: None)
    assert cj._rbac_creation_error(enabled_toolsets=["terminal"], has_script=True, is_no_agent=True) is None


def test_create_returns_rbac_error(monkeypatch):
    monkeypatch.setattr(cj, "_rbac_creation_error", lambda **kw: "DENIED-X")
    out = cj.cronjob(action="create", schedule="0 9 * * *", prompt="hi", enabled_toolsets=["terminal"])
    assert "DENIED-X" in out


def test_update_returns_rbac_error(monkeypatch):
    monkeypatch.setattr(
        cj, "resolve_job_ref",
        lambda ref: {"id": "abc", "name": "n", "enabled_toolsets": None},
    )
    monkeypatch.setattr(cj, "_rbac_creation_error", lambda **kw: "DENIED-Y")
    out = cj.cronjob(action="update", job_id="abc", enabled_toolsets=["terminal"])
    assert "DENIED-Y" in out
