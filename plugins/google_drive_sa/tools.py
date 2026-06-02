"""Google Drive tools (service-account / ADC) for Hermes.

Registered via ``plugins/google_drive_sa``. Each handler returns a JSON
string built with :func:`tools.registry.tool_result` / ``tool_error``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from plugins.google_drive_sa import client
from tools.registry import tool_error, tool_result

# Google-native (Docs/Sheets/Slides) export defaults when the caller doesn't
# specify ``export_mime``. Everything else downloads its raw bytes.
_GOOGLE_EXPORT_DEFAULTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.script": "application/vnd.google-apps.script+json",
}

# Hard cap on returned text so a large file can't blow the context window.
_MAX_TEXT_CHARS = 200_000

_LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, modifiedTime, size, parents, webViewLink)"
)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _q_escape(value: str) -> str:
    """Escape a value for a Drive ``q`` string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _drive_error(exc: Exception) -> str:
    # Import lazily — HttpError only exists once deps are installed.
    try:
        from googleapiclient.errors import HttpError
    except Exception:
        HttpError = ()
    if HttpError and isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return tool_error(
            f"Google Drive API error: {exc}",
            status_code=int(status) if status else None,
        )
    # Surface lazy-install / ADC failures with their (already actionable) text.
    return tool_error(f"Drive tool failed: {type(exc).__name__}: {exc}")


def _str(args: dict, key: str, default: str = "") -> str:
    val = args.get(key, default)
    return str(val).strip() if val is not None else default


def create_drive_file(
    name: str,
    mime_type: str,
    folder_id: str = "",
    fields: str = "id, name, mimeType, parents, webViewLink",
) -> dict:
    """Create an (empty) Drive file of *mime_type*, optionally in *folder_id*.

    Used by drive_create_folder and the Docs/Sheets ``*_create`` tools — the
    Drive API is the only create path that can drop a new Google-native file
    straight into a *shared* folder (the Docs/Sheets ``create`` endpoints
    always land in the SA's own My Drive root).
    """
    body: dict[str, Any] = {"name": name, "mimeType": mime_type}
    if folder_id:
        body["parents"] = [folder_id]
    return (
        client.get_service()
        .files()
        .create(body=body, fields=fields, supportsAllDrives=True)
        .execute()
    )


# --------------------------------------------------------------------------- #
# drive_list_files
# --------------------------------------------------------------------------- #

DRIVE_LIST_SCHEMA = {
    "name": "drive_list_files",
    "description": (
        "List or search Google Drive files/folders the service account can "
        "see (i.e. files shared with the SA's email, plus shared drives it's a "
        "member of). Combine filters, or pass a raw Drive `query`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name_contains": {
                "type": "string",
                "description": "Only return items whose name contains this substring.",
            },
            "folder_id": {
                "type": "string",
                "description": "Only return items directly inside this folder ID.",
            },
            "mime_type": {
                "type": "string",
                "description": "Filter by exact MIME type, e.g. application/pdf or "
                "application/vnd.google-apps.folder for folders.",
            },
            "query": {
                "type": "string",
                "description": "Raw Drive v3 `q` expression. Overrides the other "
                "filters when set (advanced).",
            },
            "include_trashed": {
                "type": "boolean",
                "description": "Include trashed items (default false).",
            },
            "page_size": {
                "type": "integer",
                "description": "Max results, 1-100 (default 25).",
            },
        },
        "required": [],
    },
}


def _handle_drive_list_files(args: dict, **_: Any) -> str:
    try:
        if args.get("query"):
            q = _str(args, "query")
        else:
            clauses = []
            if not bool(args.get("include_trashed")):
                clauses.append("trashed = false")
            if args.get("name_contains"):
                clauses.append(f"name contains '{_q_escape(_str(args, 'name_contains'))}'")
            if args.get("folder_id"):
                clauses.append(f"'{_q_escape(_str(args, 'folder_id'))}' in parents")
            if args.get("mime_type"):
                clauses.append(f"mimeType = '{_q_escape(_str(args, 'mime_type'))}'")
            q = " and ".join(clauses) if clauses else "trashed = false"

        try:
            page_size = int(args.get("page_size", 25))
        except (TypeError, ValueError):
            page_size = 25
        page_size = max(1, min(100, page_size))

        resp = (
            client.get_service()
            .files()
            .list(
                q=q,
                pageSize=page_size,
                fields=_LIST_FIELDS,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                corpora="allDrives",
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        files = resp.get("files", [])
        return tool_result(
            {
                "success": True,
                "query": q,
                "count": len(files),
                "files": files,
                "next_page_token": resp.get("nextPageToken"),
            }
        )
    except Exception as exc:  # noqa: BLE001 — normalize to tool_error
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# drive_read_file
# --------------------------------------------------------------------------- #

DRIVE_READ_SCHEMA = {
    "name": "drive_read_file",
    "description": (
        "Read/download a Drive file by ID. Google Docs/Sheets/Slides are "
        "exported to text/CSV automatically; other files return their bytes "
        "(UTF-8 text inline, or base64 if binary)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "The Drive file ID."},
            "export_mime": {
                "type": "string",
                "description": "Override the export MIME type for Google-native "
                "files (e.g. application/pdf, text/csv, text/plain).",
            },
        },
        "required": ["file_id"],
    },
}


def _handle_drive_read_file(args: dict, **_: Any) -> str:
    file_id = _str(args, "file_id")
    if not file_id:
        return tool_error("file_id is required")
    try:
        svc = client.get_service()
        meta = (
            svc.files()
            .get(fileId=file_id, fields="id, name, mimeType, size", supportsAllDrives=True)
            .execute()
        )
        mime = meta.get("mimeType", "")

        if mime.startswith("application/vnd.google-apps."):
            export_mime = _str(args, "export_mime") or _GOOGLE_EXPORT_DEFAULTS.get(
                mime, "text/plain"
            )
            data = svc.files().export_media(fileId=file_id, mimeType=export_mime).execute()
            effective_mime = export_mime
        else:
            data = svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
            effective_mime = mime

        if isinstance(data, str):
            data = data.encode("utf-8")

        try:
            text = data.decode("utf-8")
            truncated = len(text) > _MAX_TEXT_CHARS
            return tool_result(
                {
                    "success": True,
                    "file_id": file_id,
                    "name": meta.get("name"),
                    "mime_type": effective_mime,
                    "encoding": "text",
                    "truncated": truncated,
                    "content": text[:_MAX_TEXT_CHARS],
                }
            )
        except UnicodeDecodeError:
            return tool_result(
                {
                    "success": True,
                    "file_id": file_id,
                    "name": meta.get("name"),
                    "mime_type": effective_mime,
                    "encoding": "base64",
                    "size_bytes": len(data),
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# drive_upload
# --------------------------------------------------------------------------- #

DRIVE_UPLOAD_SCHEMA = {
    "name": "drive_upload",
    "description": (
        "Create a new file (or update an existing one by file_id) in Drive. "
        "Provide text `content` or base64 `content_base64`. To land it in a "
        "specific folder, set `folder_id` (the SA must have edit access there)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "File name (required for new files)."},
            "content": {"type": "string", "description": "UTF-8 text content."},
            "content_base64": {
                "type": "string",
                "description": "Base64-encoded bytes (use instead of `content` for binary).",
            },
            "mime_type": {
                "type": "string",
                "description": "MIME type (default text/plain).",
            },
            "folder_id": {
                "type": "string",
                "description": "Parent folder ID for a new file.",
            },
            "file_id": {
                "type": "string",
                "description": "Existing file ID to update in place instead of creating.",
            },
        },
        "required": [],
    },
}


def _handle_drive_upload(args: dict, **_: Any) -> str:
    name = _str(args, "name")
    file_id = _str(args, "file_id")
    if not name and not file_id:
        return tool_error("Provide `name` (new file) or `file_id` (update existing)")

    # Resolve content bytes.
    if args.get("content_base64"):
        try:
            data = base64.b64decode(_str(args, "content_base64"), validate=True)
        except (binascii.Error, ValueError) as exc:
            return tool_error(f"content_base64 is not valid base64: {exc}")
    elif args.get("content") is not None:
        data = str(args.get("content")).encode("utf-8")
    else:
        return tool_error("Provide `content` or `content_base64`")

    mime_type = _str(args, "mime_type") or "text/plain"
    try:
        from googleapiclient.http import MediaInMemoryUpload

        svc = client.get_service()
        media = MediaInMemoryUpload(data, mimetype=mime_type, resumable=False)
        fields = "id, name, mimeType, parents, webViewLink"

        if file_id:
            body: dict[str, Any] = {}
            if name:
                body["name"] = name
            result = (
                svc.files()
                .update(
                    fileId=file_id,
                    body=body or None,
                    media_body=media,
                    fields=fields,
                    supportsAllDrives=True,
                )
                .execute()
            )
            action = "updated"
        else:
            body: dict[str, Any] = {"name": name, "mimeType": mime_type}
            if args.get("folder_id"):
                body["parents"] = [_str(args, "folder_id")]
            result = (
                svc.files()
                .create(body=body, media_body=media, fields=fields, supportsAllDrives=True)
                .execute()
            )
            action = "created"

        return tool_result({"success": True, "action": action, "file": result})
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# drive_create_folder
# --------------------------------------------------------------------------- #

DRIVE_CREATE_FOLDER_SCHEMA = {
    "name": "drive_create_folder",
    "description": "Create a new folder in Drive, optionally inside a parent folder.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Folder name."},
            "parent_id": {
                "type": "string",
                "description": "Parent folder ID (omit for the SA's My Drive root).",
            },
        },
        "required": ["name"],
    },
}


def _handle_drive_create_folder(args: dict, **_: Any) -> str:
    name = _str(args, "name")
    if not name:
        return tool_error("name is required")
    try:
        body: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if args.get("parent_id"):
            body["parents"] = [_str(args, "parent_id")]
        result = (
            client.get_service()
            .files()
            .create(
                body=body,
                fields="id, name, parents, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return tool_result({"success": True, "folder": result})
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)
