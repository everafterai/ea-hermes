"""Unit tests for the jira_api tool. The network call (_jira_request) and creds
are mocked; these tests pin path validation, the GET-only allowlist, url/cred
plumbing, error shaping, and registration."""
import json
import pytest

import tools.jira_api_tool as jt


@pytest.fixture
def jira_ok(monkeypatch):
    monkeypatch.setattr(jt, "_jira_creds",
                        lambda: ("https://ex.atlassian.net", "me@ex.com", "tok"))


def _run(args):
    from model_tools import _run_async
    return json.loads(_run_async(jt._jira_api_handler(args)))


def test_registered_under_jira_toolset():
    from tools.registry import registry
    assert registry.get_toolset_for_tool("jira_api") == "jira"
    assert registry.get_schema("jira_api")["name"] == "jira_api"


def test_jira_toolset_declared_and_maps_to_tool():
    import toolsets
    from tools.registry import registry
    assert "jira" in toolsets.TOOLSETS
    assert toolsets.TOOLSETS["jira"]["tools"] == ["jira_api"]
    assert registry.get_tool_names_for_toolset("jira") == ["jira_api"]


def test_get_passes_path_and_parses_json(monkeypatch, jira_ok):
    captured = {}

    async def fake_req(base, email, token, path):
        captured.update(base=base, email=email, token=token, path=path)
        return {"status": 200,
                "text": '{"key": "EA-1", "fields": {"status": {"name": "Done"}}}'}

    monkeypatch.setattr(jt, "_jira_request", fake_req)
    out = _run({"path": "rest/api/3/issue/EA-1?fields=status"})
    assert out["ok"] is True
    assert out["data"]["fields"]["status"]["name"] == "Done"
    assert captured["path"] == "rest/api/3/issue/EA-1?fields=status"
    assert captured["base"] == "https://ex.atlassian.net"
    assert captured["email"] == "me@ex.com"


def test_rejects_non_get_method(monkeypatch, jira_ok):
    monkeypatch.setattr(jt, "_jira_request",
                        lambda *a, **k: pytest.fail("must not request"))
    out = _run({"path": "rest/api/3/issue/EA-1", "method": "POST"})
    assert "error" in out


def test_rejects_non_rest_path(monkeypatch, jira_ok):
    out = _run({"path": "v1/whatever"})
    assert "error" in out


def test_http_error_returns_structured_error(monkeypatch, jira_ok):
    async def fake_req(*a, **k):
        return {"status": 401, "text": "Unauthorized"}
    monkeypatch.setattr(jt, "_jira_request", fake_req)
    out = _run({"path": "rest/api/3/issue/EA-1"})
    assert "error" in out and "401" in out["error"]


def test_missing_creds_returns_structured_error(monkeypatch):
    monkeypatch.setattr(jt, "_jira_creds", lambda: ("", "", ""))
    out = _run({"path": "rest/api/3/issue/EA-1"})
    assert "error" in out and "configured" in out["error"].lower()


def test_check_fn_follows_creds(monkeypatch):
    monkeypatch.setattr(jt, "_jira_creds", lambda: ("b", "e", "t"))
    assert jt._check_jira_api() is True
    monkeypatch.setattr(jt, "_jira_creds", lambda: ("", "", ""))
    assert jt._check_jira_api() is False
