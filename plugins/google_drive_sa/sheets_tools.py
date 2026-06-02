"""Google Sheets tools (service-account / ADC) for Hermes.

Structured cell-level editing via the Sheets v4 API. The SA needs **Editor**
access on the spreadsheet (share it with the SA's email) for writes; reads
need at least Viewer.
"""

from __future__ import annotations

import json
from typing import Any

from plugins.google_drive_sa import client
from plugins.google_drive_sa.tools import _drive_error, _str, create_drive_file
from tools.registry import tool_error, tool_result

_VALUE_INPUT_OPTIONS = {"RAW", "USER_ENTERED"}


def _coerce_rows(raw: Any) -> list[list[Any]]:
    """Normalize a tool-supplied ``values`` arg into a 2D list (rows of cells).

    Accepts a JSON string, a flat list (treated as one row), or a list of
    lists (used as-is).
    """
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise ValueError("values must be a list (a row) or list of lists (rows)")
    if raw and not any(isinstance(r, list) for r in raw):
        # Flat list → single row.
        return [list(raw)]
    rows: list[list[Any]] = []
    for r in raw:
        rows.append(list(r) if isinstance(r, list) else [r])
    return rows


def _value_input_option(args: dict) -> str:
    opt = _str(args, "value_input_option", "USER_ENTERED").upper()
    return opt if opt in _VALUE_INPUT_OPTIONS else "USER_ENTERED"


# --------------------------------------------------------------------------- #
# sheets_get_values
# --------------------------------------------------------------------------- #

SHEETS_GET_VALUES_SCHEMA = {
    "name": "sheets_get_values",
    "description": "Read a range of cell values from a Google Sheet (A1 notation, "
    "e.g. 'Sheet1!A1:C10' or just 'Sheet1').",
    "parameters": {
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string", "description": "The spreadsheet ID."},
            "range": {"type": "string", "description": "A1-notation range to read."},
        },
        "required": ["spreadsheet_id", "range"],
    },
}


def _handle_sheets_get_values(args: dict, **_: Any) -> str:
    sid, rng = _str(args, "spreadsheet_id"), _str(args, "range")
    if not sid or not rng:
        return tool_error("spreadsheet_id and range are required")
    try:
        resp = (
            client.get_sheets_service()
            .spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=rng)
            .execute()
        )
        values = resp.get("values", [])
        return tool_result(
            {"success": True, "range": resp.get("range", rng), "rows": len(values), "values": values}
        )
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# sheets_update_values
# --------------------------------------------------------------------------- #

SHEETS_UPDATE_VALUES_SCHEMA = {
    "name": "sheets_update_values",
    "description": "Overwrite a range of cells in a Google Sheet with the given "
    "values (2D array of rows). Needs Editor access.",
    "parameters": {
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string", "description": "The spreadsheet ID."},
            "range": {"type": "string", "description": "A1-notation top-left anchor / target range."},
            "values": {
                "type": "array",
                "items": {"type": "array", "items": {}},
                "description": "Rows of cell values, e.g. [[\"a\",1],[\"b\",2]].",
            },
            "value_input_option": {
                "type": "string",
                "enum": ["USER_ENTERED", "RAW"],
                "description": "USER_ENTERED parses formulas/dates (default); RAW stores verbatim.",
            },
        },
        "required": ["spreadsheet_id", "range", "values"],
    },
}


def _handle_sheets_update_values(args: dict, **_: Any) -> str:
    sid, rng = _str(args, "spreadsheet_id"), _str(args, "range")
    if not sid or not rng:
        return tool_error("spreadsheet_id and range are required")
    try:
        rows = _coerce_rows(args.get("values"))
    except (ValueError, json.JSONDecodeError) as exc:
        return tool_error(f"invalid values: {exc}")
    try:
        resp = (
            client.get_sheets_service()
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=sid,
                range=rng,
                valueInputOption=_value_input_option(args),
                body={"values": rows},
            )
            .execute()
        )
        return tool_result(
            {
                "success": True,
                "updated_range": resp.get("updatedRange"),
                "updated_cells": resp.get("updatedCells"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# sheets_append_values
# --------------------------------------------------------------------------- #

SHEETS_APPEND_VALUES_SCHEMA = {
    "name": "sheets_append_values",
    "description": "Append rows after the last row of data in a Google Sheet "
    "range/table. Needs Editor access.",
    "parameters": {
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string", "description": "The spreadsheet ID."},
            "range": {"type": "string", "description": "A1-notation range/table to append to (e.g. 'Sheet1!A1')."},
            "values": {
                "type": "array",
                "items": {"type": "array", "items": {}},
                "description": "Rows of cell values to append.",
            },
            "value_input_option": {
                "type": "string",
                "enum": ["USER_ENTERED", "RAW"],
                "description": "USER_ENTERED parses formulas/dates (default); RAW stores verbatim.",
            },
        },
        "required": ["spreadsheet_id", "range", "values"],
    },
}


def _handle_sheets_append_values(args: dict, **_: Any) -> str:
    sid, rng = _str(args, "spreadsheet_id"), _str(args, "range")
    if not sid or not rng:
        return tool_error("spreadsheet_id and range are required")
    try:
        rows = _coerce_rows(args.get("values"))
    except (ValueError, json.JSONDecodeError) as exc:
        return tool_error(f"invalid values: {exc}")
    try:
        resp = (
            client.get_sheets_service()
            .spreadsheets()
            .values()
            .append(
                spreadsheetId=sid,
                range=rng,
                valueInputOption=_value_input_option(args),
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
            .execute()
        )
        updates = resp.get("updates", {})
        return tool_result(
            {
                "success": True,
                "updated_range": updates.get("updatedRange"),
                "updated_rows": updates.get("updatedRows"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# sheets_clear
# --------------------------------------------------------------------------- #

SHEETS_CLEAR_SCHEMA = {
    "name": "sheets_clear",
    "description": "Clear the values in a range of a Google Sheet (keeps formatting). "
    "Needs Editor access.",
    "parameters": {
        "type": "object",
        "properties": {
            "spreadsheet_id": {"type": "string", "description": "The spreadsheet ID."},
            "range": {"type": "string", "description": "A1-notation range to clear."},
        },
        "required": ["spreadsheet_id", "range"],
    },
}


def _handle_sheets_clear(args: dict, **_: Any) -> str:
    sid, rng = _str(args, "spreadsheet_id"), _str(args, "range")
    if not sid or not rng:
        return tool_error("spreadsheet_id and range are required")
    try:
        resp = (
            client.get_sheets_service()
            .spreadsheets()
            .values()
            .clear(spreadsheetId=sid, range=rng, body={})
            .execute()
        )
        return tool_result({"success": True, "cleared_range": resp.get("clearedRange", rng)})
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)


# --------------------------------------------------------------------------- #
# sheets_create
# --------------------------------------------------------------------------- #

SHEETS_CREATE_SCHEMA = {
    "name": "sheets_create",
    "description": "Create a new, empty Google Sheet, optionally inside a shared "
    "folder (the SA must have Editor access on that folder).",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Spreadsheet title."},
            "folder_id": {"type": "string", "description": "Parent folder ID (optional)."},
        },
        "required": ["title"],
    },
}


def _handle_sheets_create(args: dict, **_: Any) -> str:
    title = _str(args, "title")
    if not title:
        return tool_error("title is required")
    try:
        result = create_drive_file(
            title, "application/vnd.google-apps.spreadsheet", _str(args, "folder_id")
        )
        return tool_result({"success": True, "spreadsheet": result})
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)
