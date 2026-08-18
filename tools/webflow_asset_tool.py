"""webflow_asset_upload — upload a local file to Webflow's asset CDN.

The Webflow MCP server (``mcp_servers.webflow``) exposes ``asset_tool``, but its
action set is ``create_folder`` / ``get_all_assets_and_folders`` /
``update_asset`` — it can organize and rename existing assets, and it is a
Designer-session tool besides. There is NO asset-creation tool in the package
(verified against webflow-mcp-server 1.0.0, the latest published version), so an
agent that generates or receives an image has no way to get it into Webflow and
falls back to driving the dashboard through ``browser`` — which hits a login /
human-verification wall.

This tool fills that one gap through the Data API's two-leg upload:

  1. ``POST /v2/sites/{site_id}/assets`` with ``fileName`` + ``fileHash`` (md5)
     returns a presigned S3 form (``uploadUrl`` + ``uploadDetails``).
  2. multipart ``POST`` of the bytes to ``uploadUrl`` with those fields.

The agent gets back ``hosted_url`` — the CDN link to drop into a CMS item.

Credentials come from ``WEBFLOW_API_TOKEN`` in ``~/.hermes/.env`` (the same var
the MCP server block remaps to ``WEBFLOW_TOKEN``) and self-heal for headless
cron/worker runs that never loaded the dotenv — mirrors jira_api / notion_api.
The token needs the ``assets:write`` scope.

Registered as its OWN toolset (``webflow_assets``) so RBAC gates it
independently of the ``mcp-webflow`` toolset: writing bytes to a public CDN is a
different privilege from reading collections.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_WEBFLOW_API_BASE = "https://api.webflow.com/v2"
_CREATE_TIMEOUT_SECONDS = 30
_UPLOAD_TIMEOUT_SECONDS = 300
# Webflow's own per-asset ceiling is lower (4MB for images at time of writing);
# this is a local sanity bound so a stray path can't be slurped into memory.
# Anything between the two is rejected by the API with a clear message.
_MAX_ASSET_BYTES = 25 * 1024 * 1024


WEBFLOW_ASSET_UPLOAD_SCHEMA = {
    "name": "webflow_asset_upload",
    "description": (
        "Upload a LOCAL file to the Webflow asset CDN and get its public URL "
        "back. This is the ONLY way to add an asset to Webflow — the Webflow "
        "MCP server's asset_tool can list, move and rename assets but CANNOT "
        "create them. Use this for blog cover images and any other media, then "
        "put the returned hosted_url into the CMS item field. Never try to "
        "upload through the Webflow dashboard in a browser."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "site_id": {
                "type": "string",
                "description": (
                    "Webflow site id. Do not guess it — use the Webflow MCP "
                    "sites_list tool if you don't already know it."
                ),
            },
            "file_path": {
                "type": "string",
                "description": "Absolute path to the local file to upload.",
            },
            "file_name": {
                "type": "string",
                "description": (
                    "Asset file name in Webflow, including extension. "
                    "Defaults to the local file's name. Keep it under 100 chars."
                ),
            },
            "parent_folder": {
                "type": "string",
                "description": "Optional Webflow asset folder id to upload into.",
            },
        },
        "required": ["site_id", "file_path"],
    },
}


def _webflow_token() -> str:
    """Resolve the Webflow API token, self-healing from ~/.hermes/.env once.

    ``WEBFLOW_API_TOKEN`` is the canonical name (the MCP server block remaps it
    to the package's ``WEBFLOW_TOKEN``); the latter is accepted as a fallback so
    a host configured only for the MCP server still works.
    """
    def _read() -> str:
        return (
            os.getenv("WEBFLOW_API_TOKEN", "").strip()
            or os.getenv("WEBFLOW_TOKEN", "").strip()
        )

    token = _read()
    if not token:
        try:
            from hermes_cli.env_loader import load_hermes_dotenv
            load_hermes_dotenv()
        except Exception:
            pass
        token = _read()
    return token


def _local_read_denial(path: Path) -> str | None:
    """Return a denial reason if *path* must never leave this host.

    An upload publishes bytes to a CDN, so the file tool's read guards apply
    with more force here: credential stores (``.env``, ``auth.json``, …) and
    other users' session/memory data would become world-readable URLs.
    """
    try:
        from agent.file_safety import get_read_block_error, is_protected_data_path
    except Exception:  # pragma: no cover - defensive
        return None
    return get_read_block_error(str(path)) or is_protected_data_path(str(path))


async def _create_asset(
    token: str,
    site_id: str,
    file_name: str,
    file_hash: str,
    parent_folder: str,
) -> dict:
    """Leg 1: register the asset, getting a presigned S3 form back."""
    import aiohttp
    from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

    url = f"{_WEBFLOW_API_BASE}/sites/{site_id}/assets"
    payload: dict = {"fileName": file_name, "fileHash": file_hash}
    if parent_folder:
        payload["parentFolder"] = parent_folder
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(resolve_proxy_url())
    timeout = aiohttp.ClientTimeout(total=_CREATE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, **_sess_kw) as session:
        async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
            return {"status": resp.status, "text": await resp.text()}


async def _upload_file(
    upload_url: str,
    upload_details: dict,
    file_path: Path,
    file_name: str,
    content_type: str,
) -> dict:
    """Leg 2: POST the bytes to S3 as a multipart form.

    Every ``uploadDetails`` field is echoed back as a form field, and the file
    part goes LAST — S3 ignores anything after it.
    """
    import aiohttp
    from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

    form = aiohttp.FormData()
    for key, value in (upload_details or {}).items():
        form.add_field(str(key), "" if value is None else str(value))
    form.add_field(
        "file",
        Path(file_path).read_bytes(),
        filename=file_name,
        content_type=content_type,
    )
    _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(resolve_proxy_url())
    timeout = aiohttp.ClientTimeout(total=_UPLOAD_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, **_sess_kw) as session:
        async with session.post(upload_url, data=form, **_req_kw) as resp:
            return {"status": resp.status, "text": await resp.text()}


async def _webflow_asset_upload_handler(args: dict, **_kw) -> str:
    site_id = (args.get("site_id") or "").strip()
    raw_path = (args.get("file_path") or "").strip()

    if not site_id:
        return tool_error("site_id is required (use the Webflow sites_list tool).")
    if not raw_path:
        return tool_error("file_path is required.")

    path = Path(raw_path).expanduser()
    if not path.exists():
        return tool_error(f"File not found: {raw_path}")
    if not path.is_file():
        return tool_error(f"Not a file: {raw_path}")

    denial = _local_read_denial(path)
    if denial:
        return tool_error(denial)

    size = path.stat().st_size
    if size > _MAX_ASSET_BYTES:
        return tool_error(
            f"File too large to upload ({size} bytes > {_MAX_ASSET_BYTES})."
        )

    token = _webflow_token()
    if not token:
        return tool_error(
            "Webflow not configured (set WEBFLOW_API_TOKEN with the "
            "assets:write scope in ~/.hermes/.env)."
        )

    file_name = (args.get("file_name") or "").strip() or path.name
    parent_folder = (args.get("parent_folder") or "").strip()

    try:
        file_hash = hashlib.md5(path.read_bytes()).hexdigest()
    except Exception as e:
        return tool_error(f"Could not read {raw_path}: {e}")

    try:
        created = await _create_asset(
            token, site_id, file_name, file_hash, parent_folder
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[webflow_asset_upload] create failed (%s): %s", file_name, e)
        return tool_error(f"Webflow asset create failed: {e}")

    body = (created.get("text") or "").strip()
    if created.get("status", 0) >= 400:
        logger.warning(
            "[webflow_asset_upload] HTTP %s creating '%s': %s",
            created.get("status"), file_name, body[:300],
        )
        return tool_error(
            f"Webflow asset create error (HTTP {created.get('status')}): {body[:300]}"
        )

    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return tool_error(f"Webflow returned non-JSON on asset create: {body[:300]}")

    upload_url = data.get("uploadUrl")
    upload_details = data.get("uploadDetails")
    if not upload_url or not isinstance(upload_details, dict):
        return tool_error(
            "Webflow asset create returned no presigned upload form "
            "(uploadUrl/uploadDetails missing) — nothing was uploaded."
        )

    content_type = (
        upload_details.get("content-type")
        or data.get("contentType")
        or mimetypes.guess_type(file_name)[0]
        or "application/octet-stream"
    )

    try:
        uploaded = await _upload_file(
            upload_url, upload_details, path, file_name, content_type
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[webflow_asset_upload] upload failed (%s): %s", file_name, e)
        return tool_error(f"Webflow asset upload failed: {e}")

    status = uploaded.get("status", 0)
    if not (200 <= status < 300):
        detail = (uploaded.get("text") or "").strip()[:300]
        logger.warning(
            "[webflow_asset_upload] S3 HTTP %s for '%s': %s", status, file_name, detail
        )
        return tool_error(
            f"Webflow asset upload error (HTTP {status}): {detail}. The asset "
            "record exists but has no file — retry the upload."
        )

    return tool_result({
        "ok": True,
        "asset_id": data.get("id"),
        "file_name": file_name,
        "hosted_url": data.get("hostedUrl"),
        "asset_url": data.get("assetUrl"),
        "content_type": content_type,
        "size_bytes": size,
    })


def _check_webflow_asset_upload() -> bool:
    """Available whenever a Webflow API token is resolvable."""
    return bool(_webflow_token())


registry.register(
    name="webflow_asset_upload",
    toolset="webflow_assets",
    schema=WEBFLOW_ASSET_UPLOAD_SCHEMA,
    handler=lambda args, **kw: _webflow_asset_upload_handler(args, **kw),
    check_fn=_check_webflow_asset_upload,
    requires_env=[],
    is_async=True,
    emoji="🖼️",
)
