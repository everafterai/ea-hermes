"""Tests for the google_drive_sa plugin (service-account / ADC Drive tools)."""

from __future__ import annotations

import base64
import json
import sys
import types

import pytest

from plugins.google_drive_sa import client as gd_client
from plugins.google_drive_sa import tools as gd


@pytest.fixture(autouse=True)
def stub_googleapiclient_http(monkeypatch):
    """Provide googleapiclient.http.MediaInMemoryUpload when the real
    google-api-python-client isn't installed (it's lazy-installed at runtime).
    No-op when the real package is present."""
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
            self.mimetype = mimetype

    http_mod.MediaInMemoryUpload = _MediaInMemoryUpload
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http_mod)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeFiles:
    """Records the kwargs each Drive call received and returns canned data."""

    def __init__(self):
        self.calls = {}

    def list(self, **kw):
        self.calls["list"] = kw
        return _FakeRequest(
            {"files": [{"id": "f1", "name": "hello.txt"}], "nextPageToken": "tok"}
        )

    def get(self, **kw):
        self.calls["get"] = kw
        return _FakeRequest({"id": kw["fileId"], "name": "hello.txt", "mimeType": "text/plain"})

    def get_media(self, **kw):
        self.calls["get_media"] = kw
        return _FakeRequest(b"hello world")

    def export_media(self, **kw):
        self.calls["export_media"] = kw
        return _FakeRequest(b"exported,csv\n1,2")

    def create(self, **kw):
        self.calls["create"] = kw
        return _FakeRequest({"id": "new1", "name": kw["body"].get("name")})

    def update(self, **kw):
        self.calls["update"] = kw
        return _FakeRequest({"id": kw["fileId"], "name": "renamed"})


class _FakeService:
    def __init__(self):
        self._files = _FakeFiles()

    def files(self):
        return self._files


@pytest.fixture
def fake_service(monkeypatch):
    svc = _FakeService()
    monkeypatch.setattr(gd_client, "get_service", lambda: svc)
    gd_client.reset_cache()
    return svc


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_list_builds_query_from_filters(fake_service):
    out = json.loads(
        gd._handle_drive_list_files(
            {"name_contains": "budget", "folder_id": "FOLDER", "mime_type": "application/pdf"}
        )
    )
    assert out["success"] is True
    assert out["count"] == 1
    q = fake_service.files().calls["list"]["q"]
    assert "trashed = false" in q
    assert "name contains 'budget'" in q
    assert "'FOLDER' in parents" in q
    assert "mimeType = 'application/pdf'" in q
    # Shared-drive items must be visible to a service account.
    assert fake_service.files().calls["list"]["includeItemsFromAllDrives"] is True
    assert fake_service.files().calls["list"]["supportsAllDrives"] is True


def test_list_escapes_single_quotes(fake_service):
    gd._handle_drive_list_files({"name_contains": "o'brien"})
    q = fake_service.files().calls["list"]["q"]
    assert "name contains 'o\\'brien'" in q


def test_list_raw_query_overrides_filters(fake_service):
    gd._handle_drive_list_files({"query": "mimeType = 'x'", "name_contains": "ignored"})
    assert fake_service.files().calls["list"]["q"] == "mimeType = 'x'"


def test_list_page_size_clamped(fake_service):
    gd._handle_drive_list_files({"page_size": 9999})
    assert fake_service.files().calls["list"]["pageSize"] == 100


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #


def test_read_requires_file_id():
    out = json.loads(gd._handle_drive_read_file({}))
    assert "error" in out


def test_read_text_file_returns_inline_text(fake_service):
    out = json.loads(gd._handle_drive_read_file({"file_id": "f1"}))
    assert out["encoding"] == "text"
    assert out["content"] == "hello world"
    # Plain files use get_media, not export.
    assert "get_media" in fake_service.files().calls
    assert "export_media" not in fake_service.files().calls


def test_read_google_doc_exports(fake_service, monkeypatch):
    files = fake_service.files()

    def _get(**kw):
        files.calls["get"] = kw
        return _FakeRequest(
            {"id": "d1", "name": "Doc", "mimeType": "application/vnd.google-apps.spreadsheet"}
        )

    monkeypatch.setattr(files, "get", _get)
    out = json.loads(gd._handle_drive_read_file({"file_id": "d1"}))
    assert out["encoding"] == "text"
    assert out["content"] == "exported,csv\n1,2"
    # Default export for a spreadsheet is CSV.
    assert files.calls["export_media"]["mimeType"] == "text/csv"


def test_read_binary_falls_back_to_base64(fake_service, monkeypatch):
    files = fake_service.files()
    monkeypatch.setattr(files, "get_media", lambda **kw: _FakeRequest(b"\xff\xfe\x00"))
    out = json.loads(gd._handle_drive_read_file({"file_id": "bin"}))
    assert out["encoding"] == "base64"
    assert base64.b64decode(out["content_base64"]) == b"\xff\xfe\x00"


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #


def test_upload_requires_name_or_file_id():
    out = json.loads(gd._handle_drive_upload({"content": "x"}))
    assert "error" in out


def test_upload_creates_with_parent(fake_service):
    out = json.loads(
        gd._handle_drive_upload({"name": "n.txt", "content": "hi", "folder_id": "P"})
    )
    assert out["action"] == "created"
    body = fake_service.files().calls["create"]["body"]
    assert body["name"] == "n.txt"
    assert body["parents"] == ["P"]
    assert fake_service.files().calls["create"]["supportsAllDrives"] is True


def test_upload_update_existing_uses_update(fake_service):
    out = json.loads(gd._handle_drive_upload({"file_id": "f9", "content": "v2"}))
    assert out["action"] == "updated"
    assert fake_service.files().calls["update"]["fileId"] == "f9"


def test_upload_rejects_bad_base64():
    out = json.loads(gd._handle_drive_upload({"name": "x", "content_base64": "!!notb64!!"}))
    assert "error" in out


# --------------------------------------------------------------------------- #
# create_folder
# --------------------------------------------------------------------------- #


def test_create_folder_sets_mimetype(fake_service):
    out = json.loads(gd._handle_drive_create_folder({"name": "Reports", "parent_id": "P"}))
    assert out["success"] is True
    body = fake_service.files().calls["create"]["body"]
    assert body["mimeType"] == "application/vnd.google-apps.folder"
    assert body["parents"] == ["P"]


def test_create_folder_requires_name():
    out = json.loads(gd._handle_drive_create_folder({}))
    assert "error" in out


# --------------------------------------------------------------------------- #
# registration + availability
# --------------------------------------------------------------------------- #


def test_register_wires_all_tools_under_correct_toolsets():
    import plugins.google_drive_sa as plugin

    by_toolset: dict[str, set] = {}

    class _Ctx:
        def register_tool(self, **kw):
            assert callable(kw["check_fn"])
            by_toolset.setdefault(kw["toolset"], set()).add(kw["name"])

    plugin.register(_Ctx())
    assert by_toolset["google_drive"] == {
        "drive_list_files", "drive_read_file", "drive_upload", "drive_create_folder",
    }
    assert by_toolset["google_sheets"] == {
        "sheets_get_values", "sheets_update_values", "sheets_append_values",
        "sheets_clear", "sheets_create",
    }
    assert by_toolset["google_docs"] == {
        "docs_get", "docs_insert_text", "docs_replace_text", "docs_create",
    }


def test_check_available_optimistic_when_deps_absent(monkeypatch):
    gd_client.reset_cache()

    import builtins

    real_import = builtins.__import__

    def _no_google(name, *a, **k):
        if name == "google.auth" or name.startswith("google.auth"):
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_google)
    # Deps missing → still True so the toolset can be enabled and install on use.
    assert gd_client.check_available() is True


def test_toolsets_are_default_off():
    from hermes_cli.tools_config import _DEFAULT_OFF_TOOLSETS

    assert {"google_drive", "google_sheets", "google_docs"} <= _DEFAULT_OFF_TOOLSETS


# --------------------------------------------------------------------------- #
# Sheets
# --------------------------------------------------------------------------- #

from plugins.google_drive_sa import sheets_tools as sh  # noqa: E402


class _FakeValues:
    def __init__(self):
        self.calls = {}

    def get(self, **kw):
        self.calls["get"] = kw
        return _FakeRequest({"range": kw["range"], "values": [["a", "1"], ["b", "2"]]})

    def update(self, **kw):
        self.calls["update"] = kw
        return _FakeRequest({"updatedRange": kw["range"], "updatedCells": 4})

    def append(self, **kw):
        self.calls["append"] = kw
        return _FakeRequest({"updates": {"updatedRange": "S!A3", "updatedRows": 1}})

    def clear(self, **kw):
        self.calls["clear"] = kw
        return _FakeRequest({"clearedRange": kw["range"]})


class _FakeSheets:
    def __init__(self):
        self._values = _FakeValues()

    def values(self):
        return self._values


class _FakeSheetsService:
    def __init__(self):
        self._s = _FakeSheets()

    def spreadsheets(self):
        return self._s


@pytest.fixture
def fake_sheets(monkeypatch):
    svc = _FakeSheetsService()
    monkeypatch.setattr(sh.client, "get_sheets_service", lambda: svc)
    return svc


def test_sheets_get_values(fake_sheets):
    out = json.loads(sh._handle_sheets_get_values({"spreadsheet_id": "s1", "range": "S!A1:B2"}))
    assert out["rows"] == 2
    assert out["values"] == [["a", "1"], ["b", "2"]]


def test_sheets_update_coerces_flat_row(fake_sheets):
    out = json.loads(
        sh._handle_sheets_update_values(
            {"spreadsheet_id": "s1", "range": "S!A1", "values": ["x", "y"]}
        )
    )
    assert out["success"] is True
    # Flat row wrapped into a 2D body.
    assert fake_sheets.spreadsheets().values().calls["update"]["body"]["values"] == [["x", "y"]]
    assert fake_sheets.spreadsheets().values().calls["update"]["valueInputOption"] == "USER_ENTERED"


def test_sheets_update_accepts_json_string(fake_sheets):
    sh._handle_sheets_update_values(
        {"spreadsheet_id": "s1", "range": "S!A1", "values": '[[1,2],[3,4]]'}
    )
    assert fake_sheets.spreadsheets().values().calls["update"]["body"]["values"] == [[1, 2], [3, 4]]


def test_sheets_append(fake_sheets):
    out = json.loads(
        sh._handle_sheets_append_values(
            {"spreadsheet_id": "s1", "range": "S!A1", "values": [["z"]], "value_input_option": "RAW"}
        )
    )
    assert out["updated_rows"] == 1
    call = fake_sheets.spreadsheets().values().calls["append"]
    assert call["insertDataOption"] == "INSERT_ROWS"
    assert call["valueInputOption"] == "RAW"


def test_sheets_clear(fake_sheets):
    out = json.loads(sh._handle_sheets_clear({"spreadsheet_id": "s1", "range": "S!A1:B2"}))
    assert out["cleared_range"] == "S!A1:B2"


def test_sheets_update_requires_ids():
    out = json.loads(sh._handle_sheets_update_values({"values": [[1]]}))
    assert "error" in out


def test_sheets_create_uses_drive(fake_service):
    out = json.loads(sh._handle_sheets_create({"title": "Budget", "folder_id": "P"}))
    assert out["success"] is True
    body = fake_service.files().calls["create"]["body"]
    assert body["mimeType"] == "application/vnd.google-apps.spreadsheet"
    assert body["parents"] == ["P"]


# --------------------------------------------------------------------------- #
# Docs
# --------------------------------------------------------------------------- #

from plugins.google_drive_sa import docs_tools as dc  # noqa: E402


class _FakeDocuments:
    def __init__(self, doc):
        self._doc = doc
        self.calls = {}

    def get(self, **kw):
        self.calls["get"] = kw
        return _FakeRequest(self._doc)

    def batchUpdate(self, **kw):
        self.calls["batchUpdate"] = kw
        return _FakeRequest({"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]})


class _FakeDocsService:
    def __init__(self, doc):
        self._d = _FakeDocuments(doc)

    def documents(self):
        return self._d


_SAMPLE_DOC = {
    "title": "Notes",
    "body": {
        "content": [
            {"endIndex": 1, "sectionBreak": {}},
            {
                "endIndex": 13,
                "paragraph": {"elements": [{"textRun": {"content": "hello world\n"}}]},
            },
        ]
    },
}


@pytest.fixture
def fake_docs(monkeypatch):
    svc = _FakeDocsService(_SAMPLE_DOC)
    monkeypatch.setattr(dc.client, "get_docs_service", lambda: svc)
    return svc


def test_docs_get_extracts_text(fake_docs):
    out = json.loads(dc._handle_docs_get({"document_id": "d1"}))
    assert out["title"] == "Notes"
    assert out["content"] == "hello world\n"


def test_docs_insert_appends_at_computed_end(fake_docs):
    out = json.loads(dc._handle_docs_insert_text({"document_id": "d1", "text": "!"}))
    assert out["success"] is True
    # endIndex of last element is 13 → insert just before final newline at 12.
    req = fake_docs.documents().calls["batchUpdate"]["body"]["requests"][0]
    assert req["insertText"]["location"]["index"] == 12
    assert req["insertText"]["text"] == "!"


def test_docs_insert_at_start(fake_docs):
    dc._handle_docs_insert_text({"document_id": "d1", "text": "X", "location": "start"})
    req = fake_docs.documents().calls["batchUpdate"]["body"]["requests"][0]
    assert req["insertText"]["location"]["index"] == 1


def test_docs_insert_explicit_index(fake_docs):
    dc._handle_docs_insert_text({"document_id": "d1", "text": "X", "index": 5})
    req = fake_docs.documents().calls["batchUpdate"]["body"]["requests"][0]
    assert req["insertText"]["location"]["index"] == 5


def test_docs_insert_requires_text(fake_docs):
    out = json.loads(dc._handle_docs_insert_text({"document_id": "d1", "text": ""}))
    assert "error" in out


def test_docs_replace_text(fake_docs):
    out = json.loads(
        dc._handle_docs_replace_text(
            {"document_id": "d1", "find": "hello", "replace": "hi", "match_case": True}
        )
    )
    assert out["occurrences_changed"] == 3
    req = fake_docs.documents().calls["batchUpdate"]["body"]["requests"][0]
    assert req["replaceAllText"]["containsText"]["matchCase"] is True


def test_docs_create_uses_drive(fake_service):
    out = json.loads(dc._handle_docs_create({"title": "Spec"}))
    assert out["success"] is True
    assert fake_service.files().calls["create"]["body"]["mimeType"] == (
        "application/vnd.google-apps.document"
    )
