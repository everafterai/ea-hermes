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


@pytest.mark.parametrize("name,call", CREATE_ROOT_CASES, ids=[c[0] for c in CREATE_ROOT_CASES])
def test_create_in_root_reports_share_failure(services, gate, monkeypatch, name, call):
    monkeypatch.setattr(access, "share_with_requester", lambda fid, r: "File created, but sharing failed")
    out = json.loads(call())
    assert out["success"] is True
    assert "sharing failed" in out["share_warning"]


@pytest.mark.parametrize("name,call", CREATE_ROOT_CASES, ids=[c[0] for c in CREATE_ROOT_CASES])
def test_create_in_root_denies_when_requester_unresolvable(services, monkeypatch, name, call):
    """An active check with no resolvable requester must deny, not silently
    create an orphan file only the SA can see."""
    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(access, "resolve_requester", lambda: None)
    out = json.loads(call())
    assert out.get("success") is not True
    assert "could not be identified" in out.get("error", "")
    drive = services[0]
    assert drive.files().calls == []
    assert drive.permissions().created == []


def test_create_in_root_does_not_share_when_check_inactive(services, monkeypatch):
    monkeypatch.setattr(access, "is_check_active", lambda: False)
    out = json.loads(dc._handle_docs_create({"title": "t"}))
    assert out["success"] is True
    assert services[0].permissions().created == []


# --------------------------------------------------------------------------- #
# drive_list_files
# --------------------------------------------------------------------------- #

class _ListDrive(_Drive):
    def __init__(self, files, perms_by_id=None, next_page_token=None):
        super().__init__()
        self._list_files = files
        self._perms_by_id = perms_by_id or {}
        self._next_page_token = next_page_token
        self.list_kw = None
        self.perm_list_calls = []

    def files(self):
        outer = self

        class _F(_Files):
            def list(self_inner, **kw):
                outer.list_kw = kw
                resp = {"files": outer._list_files}
                if outer._next_page_token is not None:
                    resp["nextPageToken"] = outer._next_page_token
                return _Req(resp)

        f = _F()
        f.calls = self._files.calls
        return f

    def permissions(self):
        outer = self

        class _P(_Perms):
            def list(self_inner, **kw):
                outer.perm_list_calls.append(kw["fileId"])
                return _Req({"permissions": outer._perms_by_id.get(kw["fileId"], [])})

        p = _P()
        p.created = self._perms.created
        return p


@pytest.fixture
def listing(monkeypatch):
    from plugins.google_drive_sa import client as gd_client

    def _install(files, perms_by_id=None, next_page_token=None):
        d = _ListDrive(files, perms_by_id, next_page_token=next_page_token)
        monkeypatch.setattr(gd_client, "get_service", lambda: d)
        access.reset_cache()
        return d

    return _install


@pytest.fixture
def alice_active(monkeypatch):
    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(
        access, "resolve_requester",
        lambda: access.Requester(platform="slack", user_id="U1", email=ALICE),
    )
    monkeypatch.setattr(
        access, "load_access_config",
        lambda: access.AccessConfig(everyone_groups=frozenset({"all@everafter.ai"})),
    )


def test_list_requests_permissions_inline(listing, alice_active):
    d = listing([])
    gd._handle_drive_list_files({})
    assert "permissions(type,emailAddress,domain,role)" in d.list_kw["fields"]
    assert "driveId" in d.list_kw["fields"]


def test_list_filters_by_inline_acl_and_strips_permissions(listing, alice_active):
    d = listing([
        {"id": "ok", "name": "Mine", "permissions": [{"type": "user", "emailAddress": ALICE, "role": "reader"}]},
        {"id": "no", "name": "ARR", "permissions": [{"type": "group", "emailAddress": "finance@everafter.ai", "role": "reader"}]},
        {"id": "all", "name": "Handbook", "permissions": [{"type": "group", "emailAddress": "all@everafter.ai", "role": "reader"}]},
    ])
    out = json.loads(gd._handle_drive_list_files({}))
    assert [f["id"] for f in out["files"]] == ["ok", "all"]
    assert out["count"] == 2
    assert all("permissions" not in f for f in out["files"])
    assert "hidden" not in json.dumps(out)
    assert d.perm_list_calls == []


def test_list_fetches_acl_for_shared_drive_items(listing, alice_active):
    d = listing(
        [
            {"id": "sd1", "name": "A", "driveId": "D"},
            {"id": "sd2", "name": "B", "driveId": "D"},
        ],
        perms_by_id={
            "sd1": [{"type": "domain", "domain": "everafter.ai", "role": "reader"}],
            "sd2": [],
        },
    )
    out = json.loads(gd._handle_drive_list_files({}))
    assert [f["id"] for f in out["files"]] == ["sd1"]
    assert sorted(d.perm_list_calls) == ["sd1", "sd2"]


def test_list_drops_items_whose_acl_fetch_fails(listing, alice_active, monkeypatch):
    listing([{"id": "sd1", "name": "A", "driveId": "D"}])

    def _boom(file_id):
        raise RuntimeError("403")

    monkeypatch.setattr(access, "fetch_acl", _boom)
    out = json.loads(gd._handle_drive_list_files({}))
    assert out["files"] == []


def test_list_returns_nothing_when_active_but_no_requester(listing, monkeypatch):
    listing([{"id": "x", "name": "X", "permissions": [{"type": "anyone", "role": "reader"}]}])
    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(access, "resolve_requester", lambda: None)
    out = json.loads(gd._handle_drive_list_files({}))
    assert out["files"] == []


def test_list_unfiltered_when_check_inactive(listing, monkeypatch):
    listing([{"id": "x", "name": "X", "permissions": [{"type": "user", "emailAddress": "z@z", "role": "reader"}]}])
    monkeypatch.setattr(access, "is_check_active", lambda: False)
    out = json.loads(gd._handle_drive_list_files({}))
    assert [f["id"] for f in out["files"]] == ["x"]
    assert "permissions" not in out["files"][0]


def test_list_primes_cache_for_following_read(listing, alice_active, monkeypatch):
    d = listing([{"id": "ok", "name": "Mine", "permissions": [{"type": "user", "emailAddress": ALICE, "role": "reader"}]}])
    gd._handle_drive_list_files({})
    name, acl = access.fetch_acl("ok")
    assert name == "Mine" and acl[0]["emailAddress"] == ALICE
    assert d.files().calls == []  # served from cache, no files.get


def test_list_hides_next_page_token_when_check_active(listing, alice_active):
    """drive_list_files takes no page_token input, so a raw nextPageToken
    can't be used to page — it would only signal "more matches exist" for
    files filtered out of `files`, an existence oracle over content the
    requester can't see."""
    listing([], next_page_token="tok")
    out = json.loads(gd._handle_drive_list_files({}))
    assert out["next_page_token"] is None


def test_list_keeps_next_page_token_when_check_inactive(listing, monkeypatch):
    monkeypatch.setattr(access, "is_check_active", lambda: False)
    listing([], next_page_token="tok")
    out = json.loads(gd._handle_drive_list_files({}))
    assert out["next_page_token"] == "tok"


def test_list_resolves_requester_on_caller_thread_not_pool_workers(listing, monkeypatch):
    """Pins that the requester is resolved once on the caller's thread before
    fanning ACL fetches out to the pool — not re-resolved (or silently
    unresolved) inside each worker."""
    from gateway.session_context import set_session_vars, clear_session_vars

    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(access, "load_access_config", lambda: access.AccessConfig())

    def _resolve_email(platform, user_id):
        assert (platform, user_id) == ("slack", "U1")
        return ALICE

    monkeypatch.setattr(access, "_resolve_email", _resolve_email)

    listing(
        [
            {"id": "sd1", "name": "A", "driveId": "D"},
            {"id": "sd2", "name": "B", "driveId": "D"},
        ],
        perms_by_id={
            "sd1": [{"type": "user", "emailAddress": ALICE, "role": "reader"}],
            "sd2": [],
        },
    )
    tokens = set_session_vars(platform="slack", user_id="U1")
    try:
        out = json.loads(gd._handle_drive_list_files({}))
    finally:
        clear_session_vars(tokens)
    assert [f["id"] for f in out["files"]] == ["sd1"]
