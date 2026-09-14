# tests/plugins/test_google_drive_sa_gated_handlers.py
"""Every Drive/Sheets/Docs handler must call the per-user access gate."""

from __future__ import annotations

import json
import sys
import types

import pytest

from plugins.google_drive_sa import access
from plugins.google_drive_sa import docs_tools as dc
from plugins.google_drive_sa import sheets_tools as sh
from plugins.google_drive_sa import tools as gd

ALICE = "alice@everafter.ai"


@pytest.fixture(autouse=True)
def stub_googleapiclient_http(monkeypatch):
    try:  # pragma: no cover - depends on env
        import googleapiclient.http  # noqa: F401
        return
    except Exception:
        pass
    pkg = sys.modules.get("googleapiclient") or types.ModuleType("googleapiclient")
    http_mod = types.ModuleType("googleapiclient.http")

    class _MediaInMemoryUpload:
        def __init__(self, body, mimetype="application/octet-stream", resumable=False):
            self.body = body

    http_mod.MediaInMemoryUpload = _MediaInMemoryUpload
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http_mod)


class _Req:
    def __init__(self, r):
        self._r = r

    def execute(self):
        return self._r


class _Files:
    def __init__(self):
        self.calls = []

    def get(self, **kw):
        self.calls.append(("get", kw))
        return _Req({"id": kw["fileId"], "name": "n", "mimeType": "text/plain"})

    def get_media(self, **kw):
        self.calls.append(("get_media", kw))
        return _Req(b"secret")

    def export_media(self, **kw):
        self.calls.append(("export_media", kw))
        return _Req(b"secret")

    def create(self, **kw):
        self.calls.append(("create", kw))
        return _Req({"id": "new1", "name": kw["body"].get("name")})

    def update(self, **kw):
        self.calls.append(("update", kw))
        return _Req({"id": kw["fileId"]})


class _Perms:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return _Req({"id": "p1"})


class _Drive:
    def __init__(self):
        self._files = _Files()
        self._perms = _Perms()

    def files(self):
        return self._files

    def permissions(self):
        return self._perms


class _Values:
    def __init__(self):
        self.calls = []

    def _rec(self, name, kw, result):
        self.calls.append((name, kw))
        return _Req(result)

    def get(self, **kw):
        return self._rec("get", kw, {"range": kw["range"], "values": [["x"]]})

    def update(self, **kw):
        return self._rec("update", kw, {"updatedRange": kw["range"], "updatedCells": 1})

    def append(self, **kw):
        return self._rec("append", kw, {"updates": {"updatedRange": "S!A2", "updatedRows": 1}})

    def clear(self, **kw):
        return self._rec("clear", kw, {"clearedRange": kw["range"]})


class _Sheets:
    def __init__(self):
        self._v = _Values()

    def spreadsheets(self):
        s = self

        class _S:
            def values(self_inner):
                return s._v

        return _S()


class _Docs:
    def __init__(self):
        self.calls = []

    def documents(self):
        d = self

        class _D:
            def get(self_inner, **kw):
                d.calls.append(("get", kw))
                return _Req({"title": "T", "body": {"content": [
                    {"endIndex": 6, "paragraph": {"elements": [{"textRun": {"content": "hi\n"}}]}}]}})

            def batchUpdate(self_inner, **kw):
                d.calls.append(("batchUpdate", kw))
                return _Req({"replies": [{"replaceAllText": {"occurrencesChanged": 1}}]})

        return _D()


@pytest.fixture
def services(monkeypatch):
    from plugins.google_drive_sa import client as gd_client

    drive, sheets, docs = _Drive(), _Sheets(), _Docs()
    monkeypatch.setattr(gd_client, "get_service", lambda: drive)
    monkeypatch.setattr(gd_client, "get_sheets_service", lambda: sheets)
    monkeypatch.setattr(gd_client, "get_docs_service", lambda: docs)
    return drive, sheets, docs


@pytest.fixture
def gate(monkeypatch):
    """Record require_access calls; deny when ``deny`` is set."""
    state = {"calls": [], "deny": False}

    def _require(file_id, level):
        state["calls"].append((file_id, level))
        if state["deny"]:
            raise access.DriveAccessDenied("Access denied: nope")
        return access.Requester(platform="slack", user_id="U1", email=ALICE)

    monkeypatch.setattr(access, "require_access", _require)
    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(
        access, "resolve_requester",
        lambda: access.Requester(platform="slack", user_id="U1", email=ALICE),
    )
    return state


READ_CASES = [
    ("drive_read_file", lambda: gd._handle_drive_read_file({"file_id": "F"}), "F"),
    ("sheets_get_values", lambda: sh._handle_sheets_get_values({"spreadsheet_id": "S", "range": "A1"}), "S"),
    ("docs_get", lambda: dc._handle_docs_get({"document_id": "D"}), "D"),
]

WRITE_CASES = [
    ("sheets_update_values", lambda: sh._handle_sheets_update_values({"spreadsheet_id": "S", "range": "A1", "values": [["1"]]}), "S"),
    ("sheets_append_values", lambda: sh._handle_sheets_append_values({"spreadsheet_id": "S", "range": "A1", "values": [["1"]]}), "S"),
    ("sheets_clear", lambda: sh._handle_sheets_clear({"spreadsheet_id": "S", "range": "A1"}), "S"),
    ("docs_insert_text", lambda: dc._handle_docs_insert_text({"document_id": "D", "text": "x"}), "D"),
    ("docs_replace_text", lambda: dc._handle_docs_replace_text({"document_id": "D", "find": "a"}), "D"),
    ("drive_upload_update", lambda: gd._handle_drive_upload({"file_id": "F", "content": "x"}), "F"),
]

PARENT_CASES = [
    ("drive_upload_new", lambda: gd._handle_drive_upload({"name": "n", "content": "x", "folder_id": "P"}), "P"),
    ("drive_create_folder", lambda: gd._handle_drive_create_folder({"name": "n", "parent_id": "P"}), "P"),
    ("sheets_create", lambda: sh._handle_sheets_create({"title": "t", "folder_id": "P"}), "P"),
    ("docs_create", lambda: dc._handle_docs_create({"title": "t", "folder_id": "P"}), "P"),
]


@pytest.mark.parametrize("name,call,target", READ_CASES, ids=[c[0] for c in READ_CASES])
def test_read_tools_require_reader(services, gate, name, call, target):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == [(target, access.READER)]


@pytest.mark.parametrize("name,call,target", WRITE_CASES, ids=[c[0] for c in WRITE_CASES])
def test_write_tools_require_writer(services, gate, name, call, target):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == [(target, access.WRITER)]


@pytest.mark.parametrize("name,call,target", PARENT_CASES, ids=[c[0] for c in PARENT_CASES])
def test_create_in_folder_requires_writer_on_parent(services, gate, name, call, target):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == [(target, access.WRITER)]
    # In a folder: inheritance does the sharing; no permissions.create.
    assert services[0].permissions().created == []


@pytest.mark.parametrize(
    "name,call,target",
    READ_CASES + WRITE_CASES + PARENT_CASES,
    ids=[c[0] for c in READ_CASES + WRITE_CASES + PARENT_CASES],
)
def test_denied_tools_make_no_api_call(services, gate, name, call, target):
    gate["deny"] = True
    out = json.loads(call())
    assert out.get("success") is not True
    assert "Access denied" in out.get("error", "")
    drive, sheets, docs = services
    assert drive.files().calls == []
    assert sheets.spreadsheets().values().calls == []
    assert docs.calls == []


CREATE_ROOT_CASES = [
    ("drive_upload_new", lambda: gd._handle_drive_upload({"name": "n", "content": "x"})),
    ("drive_create_folder", lambda: gd._handle_drive_create_folder({"name": "n"})),
    ("sheets_create", lambda: sh._handle_sheets_create({"title": "t"})),
    ("docs_create", lambda: dc._handle_docs_create({"title": "t"})),
]


@pytest.mark.parametrize("name,call", CREATE_ROOT_CASES, ids=[c[0] for c in CREATE_ROOT_CASES])
def test_create_in_root_shares_with_requester(services, gate, name, call):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == []  # nothing to pre-check
    created = services[0].permissions().created
    assert len(created) == 1
    assert created[0]["fileId"] == "new1"
    assert created[0]["body"]["emailAddress"] == ALICE
    assert created[0]["body"]["role"] == "writer"


def test_create_in_root_reports_share_failure(services, gate, monkeypatch):
    monkeypatch.setattr(access, "share_with_requester", lambda fid, r: "File created, but sharing failed")
    out = json.loads(sh._handle_sheets_create({"title": "t"}))
    assert out["success"] is True
    assert "sharing failed" in out["share_warning"]


def test_create_in_root_does_not_share_when_check_inactive(services, monkeypatch):
    monkeypatch.setattr(access, "is_check_active", lambda: False)
    out = json.loads(dc._handle_docs_create({"title": "t"}))
    assert out["success"] is True
    assert services[0].permissions().created == []
