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


def test_register_wires_four_tools():
    import plugins.google_drive_sa as plugin

    names = []

    class _Ctx:
        def register_tool(self, **kw):
            names.append(kw["name"])
            assert kw["toolset"] == "google_drive"
            assert callable(kw["check_fn"])

    plugin.register(_Ctx())
    assert set(names) == {
        "drive_list_files",
        "drive_read_file",
        "drive_upload",
        "drive_create_folder",
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


def test_toolset_is_default_off():
    from hermes_cli.tools_config import _DEFAULT_OFF_TOOLSETS

    assert "google_drive" in _DEFAULT_OFF_TOOLSETS
