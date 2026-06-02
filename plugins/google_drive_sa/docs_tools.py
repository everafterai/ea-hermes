"""Google Docs tools (service-account / ADC) for Hermes.

Structured text editing via the Docs v1 API (``documents.batchUpdate``). The
SA needs **Editor** access on the document for writes; reads need Viewer.
"""

from __future__ import annotations

from typing import Any

from plugins.google_drive_sa import client
from plugins.google_drive_sa.tools import _MAX_TEXT_CHARS, _drive_error, _str, create_drive_file
from tools.registry import tool_error, tool_result


def _extract_text(doc: dict) -> str:
    """Flatten a Docs document body into plain text."""
    out: list[str] = []
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        for run in para.get("elements", []):
            text_run = run.get("textRun")
            if text_run and "content" in text_run:
                out.append(text_run["content"])
    return "".join(out)


def _end_index(doc: dict) -> int:
    """The last insertable index in the doc body (just before the final newline)."""
    content = doc.get("body", {}).get("content", [])
    if not content:
        return 1
    end = content[-1].get("endIndex", 2)
    # endIndex points one past the final segment's newline; insert before it.
    return max(1, end - 1)


# --------------------------------------------------------------------------- #
# docs_get
# --------------------------------------------------------------------------- #

DOCS_GET_SCHEMA = {
    "name": "docs_get",
    "description": "Read the full plain text of a Google Doc by ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "The document ID."},
        },
        "required": ["document_id"],
    },
}


def _handle_docs_get(args: dict, **_: Any) -> str:
    doc_id = _str(args, "document_id")
    if not doc_id:
        return tool_error("document_id is required")
    try:
        doc = client.get_docs_service().documents().get(documentId=doc_id).execute()
        text = _extract_text(doc)
        return tool_result(
            {
                "success": True,
                "document_id": doc_id,
                "title": doc.get("title"),
                "truncated": len(text) > _MAX_TEXT_CHARS,
                "content": text[:_MAX_TEXT_CHARS],
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# docs_insert_text
# --------------------------------------------------------------------------- #

DOCS_INSERT_TEXT_SCHEMA = {
    "name": "docs_insert_text",
    "description": "Insert text into a Google Doc. By default appends to the end; "
    "set location='start' or an explicit numeric index. Needs Editor access.",
    "parameters": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "The document ID."},
            "text": {"type": "string", "description": "Text to insert."},
            "location": {
                "type": "string",
                "enum": ["end", "start"],
                "description": "Where to insert when `index` is omitted (default end).",
            },
            "index": {
                "type": "integer",
                "description": "Explicit 1-based insert index (overrides `location`).",
            },
        },
        "required": ["document_id", "text"],
    },
}


def _handle_docs_insert_text(args: dict, **_: Any) -> str:
    doc_id = _str(args, "document_id")
    text = args.get("text")
    if not doc_id:
        return tool_error("document_id is required")
    if text is None or text == "":
        return tool_error("text is required")
    try:
        svc = client.get_docs_service()
        if args.get("index") is not None:
            try:
                index = max(1, int(args["index"]))
            except (TypeError, ValueError):
                return tool_error("index must be an integer")
        elif _str(args, "location", "end") == "start":
            index = 1
        else:
            doc = svc.documents().get(documentId=doc_id).execute()
            index = _end_index(doc)

        requests = [{"insertText": {"location": {"index": index}, "text": str(text)}}]
        svc.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        return tool_result({"success": True, "document_id": doc_id, "inserted_at": index})
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# docs_replace_text
# --------------------------------------------------------------------------- #

DOCS_REPLACE_TEXT_SCHEMA = {
    "name": "docs_replace_text",
    "description": "Find and replace all occurrences of a string in a Google Doc. "
    "Needs Editor access.",
    "parameters": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "The document ID."},
            "find": {"type": "string", "description": "Text to search for."},
            "replace": {"type": "string", "description": "Replacement text."},
            "match_case": {
                "type": "boolean",
                "description": "Case-sensitive match (default false).",
            },
        },
        "required": ["document_id", "find", "replace"],
    },
}


def _handle_docs_replace_text(args: dict, **_: Any) -> str:
    doc_id, find = _str(args, "document_id"), _str(args, "find")
    if not doc_id or not find:
        return tool_error("document_id and find are required")
    replace = args.get("replace", "")
    try:
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": find, "matchCase": bool(args.get("match_case"))},
                    "replaceText": str(replace),
                }
            }
        ]
        resp = (
            client.get_docs_service()
            .documents()
            .batchUpdate(documentId=doc_id, body={"requests": requests})
            .execute()
        )
        replies = resp.get("replies", [{}])
        occurrences = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
        return tool_result(
            {"success": True, "document_id": doc_id, "occurrences_changed": occurrences}
        )
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# docs_create
# --------------------------------------------------------------------------- #

DOCS_CREATE_SCHEMA = {
    "name": "docs_create",
    "description": "Create a new, empty Google Doc, optionally inside a shared "
    "folder (the SA must have Editor access on that folder).",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title."},
            "folder_id": {"type": "string", "description": "Parent folder ID (optional)."},
        },
        "required": ["title"],
    },
}


def _handle_docs_create(args: dict, **_: Any) -> str:
    title = _str(args, "title")
    if not title:
        return tool_error("title is required")
    try:
        result = create_drive_file(
            title, "application/vnd.google-apps.document", _str(args, "folder_id")
        )
        return tool_result({"success": True, "document": result})
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)
