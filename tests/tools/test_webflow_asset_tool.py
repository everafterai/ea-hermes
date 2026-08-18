"""Unit tests for the webflow_asset_upload tool.

The two HTTP legs (_create_asset → Webflow Data API, _upload_file → the
presigned S3 POST) and the token are mocked; these tests pin the hash/filename
derivation, the hand-off of uploadUrl/uploadDetails between the legs, the
local-file read guards, error shaping, and registration.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

import tools.webflow_asset_tool as wa


@pytest.fixture
def token_ok(monkeypatch):
    monkeypatch.setattr(wa, "_webflow_token", lambda: "wf-token")


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "cover.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n fake bytes")
    return p


def _run(args):
    from model_tools import _run_async
    return json.loads(_run_async(wa._webflow_asset_upload_handler(args)))


def _stub_legs(monkeypatch, *, create=None, upload=None, captured=None):
    """Install both HTTP legs, recording their arguments into *captured*."""
    captured = captured if captured is not None else {}

    async def fake_create(token, site_id, file_name, file_hash, parent_folder):
        captured.update(
            token=token, site_id=site_id, file_name=file_name,
            file_hash=file_hash, parent_folder=parent_folder,
        )
        return create if create is not None else {
            "status": 200,
            "text": json.dumps({
                "id": "asset-1",
                "uploadUrl": "https://s3.example/bucket",
                "uploadDetails": {"key": "k", "X-Amz-Signature": "sig"},
                "hostedUrl": "https://cdn.example/cover.png",
                "assetUrl": "https://s3.example/bucket/cover.png",
                "contentType": "image/png",
            }),
        }

    async def fake_upload(upload_url, upload_details, file_path, file_name, content_type):
        captured.update(
            upload_url=upload_url, upload_details=upload_details,
            upload_path=str(file_path), upload_name=file_name,
            content_type=content_type,
        )
        return upload if upload is not None else {"status": 204, "text": ""}

    monkeypatch.setattr(wa, "_create_asset", fake_create)
    monkeypatch.setattr(wa, "_upload_file", fake_upload)
    return captured


def test_registered_under_webflow_assets_toolset():
    from tools.registry import registry
    assert registry.get_toolset_for_tool("webflow_asset_upload") == "webflow_assets"
    assert registry.get_schema("webflow_asset_upload")["name"] == "webflow_asset_upload"


def test_webflow_assets_toolset_declared_and_maps_to_tool():
    import toolsets
    from tools.registry import registry
    assert "webflow_assets" in toolsets.TOOLSETS
    assert toolsets.TOOLSETS["webflow_assets"]["tools"] == ["webflow_asset_upload"]
    assert registry.get_tool_names_for_toolset("webflow_assets") == ["webflow_asset_upload"]


def test_uploads_file_and_returns_cdn_url(monkeypatch, token_ok, png):
    captured = _stub_legs(monkeypatch)

    out = _run({"site_id": "site-1", "file_path": str(png)})

    assert out["ok"] is True
    assert out["hosted_url"] == "https://cdn.example/cover.png"
    assert out["asset_id"] == "asset-1"
    # Leg 1 gets the md5 of the real bytes and the basename as the file name.
    assert captured["file_hash"] == hashlib.md5(png.read_bytes()).hexdigest()
    assert captured["file_name"] == "cover.png"
    assert captured["site_id"] == "site-1"
    assert captured["token"] == "wf-token"
    # Leg 2 gets exactly what leg 1 handed back.
    assert captured["upload_url"] == "https://s3.example/bucket"
    assert captured["upload_details"] == {"key": "k", "X-Amz-Signature": "sig"}
    assert captured["upload_path"] == str(png)


def test_explicit_file_name_and_parent_folder_are_passed_through(monkeypatch, token_ok, png):
    captured = _stub_legs(monkeypatch)

    out = _run({
        "site_id": "site-1",
        "file_path": str(png),
        "file_name": "hero-q3.png",
        "parent_folder": "folder-9",
    })

    assert out["ok"] is True
    assert captured["file_name"] == "hero-q3.png"
    assert captured["parent_folder"] == "folder-9"
    assert captured["upload_name"] == "hero-q3.png"


def test_missing_file_errors_without_calling_webflow(monkeypatch, token_ok, tmp_path):
    monkeypatch.setattr(wa, "_create_asset",
                        lambda *a, **k: pytest.fail("must not call Webflow"))
    out = _run({"site_id": "site-1", "file_path": str(tmp_path / "nope.png")})
    assert "error" in out and "not found" in out["error"].lower()


def test_directory_path_errors(monkeypatch, token_ok, tmp_path):
    monkeypatch.setattr(wa, "_create_asset",
                        lambda *a, **k: pytest.fail("must not call Webflow"))
    out = _run({"site_id": "site-1", "file_path": str(tmp_path)})
    assert "error" in out


def test_refuses_to_upload_another_users_session_data(monkeypatch, token_ok):
    """A CDN upload is public — the cross-user data guard must apply here too."""
    monkeypatch.setattr(wa, "_create_asset",
                        lambda *a, **k: pytest.fail("must not call Webflow"))
    state_db = Path(os.environ["HERMES_HOME"]) / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.write_bytes(b"sqlite")

    out = _run({"site_id": "site-1", "file_path": str(state_db)})
    assert "error" in out


def test_refuses_to_upload_credential_files(monkeypatch, token_ok):
    monkeypatch.setattr(wa, "_create_asset",
                        lambda *a, **k: pytest.fail("must not call Webflow"))
    env_file = Path(os.environ["HERMES_HOME"]) / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("WEBFLOW_API_TOKEN=secret\n", encoding="utf-8")

    out = _run({"site_id": "site-1", "file_path": str(env_file)})
    assert "error" in out


def test_oversized_file_errors_before_upload(monkeypatch, token_ok, png):
    monkeypatch.setattr(wa, "_MAX_ASSET_BYTES", 4)
    monkeypatch.setattr(wa, "_create_asset",
                        lambda *a, **k: pytest.fail("must not call Webflow"))
    out = _run({"site_id": "site-1", "file_path": str(png)})
    assert "error" in out and "too large" in out["error"].lower()


def test_create_step_http_error_is_structured(monkeypatch, token_ok, png):
    _stub_legs(monkeypatch, create={"status": 401, "text": "Unauthorized"})
    out = _run({"site_id": "site-1", "file_path": str(png)})
    assert "error" in out and "401" in out["error"]


def test_upload_step_failure_is_structured(monkeypatch, token_ok, png):
    _stub_legs(monkeypatch, upload={"status": 403, "text": "SignatureDoesNotMatch"})
    out = _run({"site_id": "site-1", "file_path": str(png)})
    assert "error" in out and "403" in out["error"]


def test_missing_upload_url_is_structured(monkeypatch, token_ok, png):
    """Webflow returning 200 without the presigned form must not crash."""
    _stub_legs(monkeypatch, create={
        "status": 200,
        "text": json.dumps({"id": "asset-1", "hostedUrl": "https://cdn.example/x.png"}),
    })
    out = _run({"site_id": "site-1", "file_path": str(png)})
    assert "error" in out


def test_missing_token_returns_structured_error(monkeypatch, png):
    monkeypatch.setattr(wa, "_webflow_token", lambda: "")
    out = _run({"site_id": "site-1", "file_path": str(png)})
    assert "error" in out and "configured" in out["error"].lower()


def test_missing_site_id_errors(monkeypatch, token_ok, png):
    monkeypatch.setattr(wa, "_create_asset",
                        lambda *a, **k: pytest.fail("must not call Webflow"))
    out = _run({"file_path": str(png)})
    assert "error" in out


def test_check_fn_follows_token(monkeypatch):
    monkeypatch.setattr(wa, "_webflow_token", lambda: "tok")
    assert wa._check_webflow_asset_upload() is True
    monkeypatch.setattr(wa, "_webflow_token", lambda: "")
    assert wa._check_webflow_asset_upload() is False
