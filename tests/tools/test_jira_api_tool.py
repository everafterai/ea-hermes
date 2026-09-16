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


# ── jira_api_write (separate `jira_write` toolset, curated allowlist) ──────────

def _run_write(args):
    from model_tools import _run_async
    return json.loads(_run_async(jt._jira_api_write_handler(args)))


@pytest.fixture
def capture_write(monkeypatch, jira_ok):
    captured = {}

    async def fake_req(base, email, token, path, method="GET", json_body=None):
        captured.update(base=base, path=path, method=method, body=json_body)
        return {"status": 200, "text": '{"ok": 1}'}

    monkeypatch.setattr(jt, "_jira_request", fake_req)
    return captured


@pytest.fixture
def never_request(monkeypatch, jira_ok):
    monkeypatch.setattr(jt, "_jira_request",
                        lambda *a, **k: pytest.fail("must not request"))


def test_write_registered_under_its_own_toolset():
    import toolsets
    from tools.registry import registry
    assert registry.get_toolset_for_tool("jira_api_write") == "jira_write"
    assert toolsets.TOOLSETS["jira_write"]["tools"] == ["jira_api_write"]
    assert registry.get_tool_names_for_toolset("jira_write") == ["jira_api_write"]
    # The read toolset must not grow: granting `jira` never implies writes.
    assert registry.get_tool_names_for_toolset("jira") == ["jira_api"]


def test_write_transition_posts_json_body(capture_write):
    out = _run_write({"path": "rest/api/3/issue/BA-12/transitions", "method": "POST",
                      "body": {"transition": {"id": "31"}}})
    assert out["ok"] is True
    assert capture_write["method"] == "POST"
    assert capture_write["path"] == "rest/api/3/issue/BA-12/transitions"
    assert capture_write["body"] == {"transition": {"id": "31"}}


@pytest.mark.parametrize("method,path", [
    ("POST", "rest/api/3/issue/BA-12/comment"),
    ("PUT", "rest/api/3/issue/BA-12"),
    ("POST", "rest/api/3/issue"),
    ("POST", "rest/api/3/issue/EA_2-7/transitions"),   # underscore + digit in project key
])
def test_write_allowlist_accepts(capture_write, method, path):
    out = _run_write({"path": path, "method": method, "body": {}})
    assert out["ok"] is True
    assert capture_write["method"] == method


@pytest.mark.parametrize("method,path", [
    ("DELETE", "rest/api/3/issue/BA-12"),                       # never delete
    ("GET", "rest/api/3/issue/BA-12/transitions"),              # reads go through jira_api
    ("POST", "rest/api/3/project"),                             # admin surface
    ("PUT", "rest/api/3/project/BA"),
    ("POST", "rest/api/3/issue/BA-12/transitions?x=1"),         # no query string
    ("POST", "rest/api/3/issue/10005/transitions"),             # numeric id, not a key
    ("POST", "rest/api/3/issue/BA-12/comment/../../project"),   # traversal
    ("POST", "rest/api/3/issue/bulk"),                          # bulk create
    ("POST", "rest/api/3/issue/BA-12/attachments"),
    ("PUT", "rest/api/3/issue/BA-12/comment/1"),                # editing comments not offered
    ("POST", "v1/issue"),                                       # not a rest/ path
])
def test_write_allowlist_rejects(never_request, method, path):
    out = _run_write({"path": path, "method": method, "body": {}})
    assert "error" in out
    assert "transitions" in out["error"]   # the error names what IS allowed


def test_write_requires_object_body(never_request):
    out = _run_write({"path": "rest/api/3/issue/BA-12/comment", "method": "POST",
                      "body": "not an object"})
    assert "error" in out


def test_write_http_error_returns_structured_error(monkeypatch, jira_ok):
    async def fake_req(*a, **k):
        return {"status": 400, "text": '{"errorMessages":["Transition 99 is not valid"]}'}
    monkeypatch.setattr(jt, "_jira_request", fake_req)
    out = _run_write({"path": "rest/api/3/issue/BA-12/transitions", "method": "POST",
                      "body": {"transition": {"id": "99"}}})
    assert "error" in out and "400" in out["error"] and "not valid" in out["error"]


def test_write_204_no_body_is_ok(monkeypatch, jira_ok):
    async def fake_req(*a, **k):
        return {"status": 204, "text": ""}
    monkeypatch.setattr(jt, "_jira_request", fake_req)
    out = _run_write({"path": "rest/api/3/issue/BA-12/transitions", "method": "POST",
                      "body": {"transition": {"id": "31"}}})
    assert out == {"ok": True, "data": None}


def test_write_missing_creds_returns_structured_error(monkeypatch):
    monkeypatch.setattr(jt, "_jira_creds", lambda: ("", "", ""))
    out = _run_write({"path": "rest/api/3/issue/BA-12/comment", "method": "POST", "body": {}})
    assert "error" in out and "configured" in out["error"].lower()


def test_read_tool_request_helper_still_defaults_to_get(capture_write):
    # The shared helper grew method/json_body; the read path must be unchanged.
    out = _run({"path": "rest/api/3/issue/BA-12/transitions"})
    assert out["ok"] is True
    assert capture_write["method"] == "GET"
    assert capture_write["body"] is None
