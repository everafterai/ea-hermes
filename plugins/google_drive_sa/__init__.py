"""Google Drive / Sheets / Docs (service account / ADC) plugin — bundled, auto-loaded.

Registers 13 tools across three default-off toolsets: ``google_drive`` (4),
``google_sheets`` (5), ``google_docs`` (4). Auth is the *service account
itself* via Application Default Credentials (``google.auth.default``) — on a
GCP VM that's the attached workload identity, so no key files. Share the
target file/folder with the SA's email (Editor for writes) to grant access.

Why ``kind: backend`` for a tool-providing plugin? The bundled plugin loader
only auto-loads ``backend`` and ``platform`` kinds without an explicit
``plugins.enabled`` opt-in (see ``hermes_cli/plugins.py``). Spotify does the
same. The ``google_drive`` toolset itself is default-off (``hermes_cli/
tools_config.py``), so tools register at startup but stay invisible to the
model until the user enables the toolset via ``hermes tools``; the per-tool
``check_fn`` then gates dispatch on ADC actually being resolvable.
"""

from __future__ import annotations

from plugins.google_drive_sa.client import check_available
from plugins.google_drive_sa.docs_tools import (
    DOCS_CREATE_SCHEMA,
    DOCS_GET_SCHEMA,
    DOCS_INSERT_TEXT_SCHEMA,
    DOCS_REPLACE_TEXT_SCHEMA,
    _handle_docs_create,
    _handle_docs_get,
    _handle_docs_insert_text,
    _handle_docs_replace_text,
)
from plugins.google_drive_sa.sheets_tools import (
    SHEETS_APPEND_VALUES_SCHEMA,
    SHEETS_CLEAR_SCHEMA,
    SHEETS_CREATE_SCHEMA,
    SHEETS_GET_VALUES_SCHEMA,
    SHEETS_UPDATE_VALUES_SCHEMA,
    _handle_sheets_append_values,
    _handle_sheets_clear,
    _handle_sheets_create,
    _handle_sheets_get_values,
    _handle_sheets_update_values,
)
from plugins.google_drive_sa.tools import (
    DRIVE_CREATE_FOLDER_SCHEMA,
    DRIVE_LIST_SCHEMA,
    DRIVE_READ_SCHEMA,
    DRIVE_UPLOAD_SCHEMA,
    _handle_drive_create_folder,
    _handle_drive_list_files,
    _handle_drive_read_file,
    _handle_drive_upload,
)

# (toolset, name, schema, handler, emoji)
_TOOLS = (
    ("google_drive",  "drive_list_files",     DRIVE_LIST_SCHEMA,           _handle_drive_list_files,     "📁"),
    ("google_drive",  "drive_read_file",      DRIVE_READ_SCHEMA,           _handle_drive_read_file,      "📄"),
    ("google_drive",  "drive_upload",         DRIVE_UPLOAD_SCHEMA,         _handle_drive_upload,         "⬆️"),
    ("google_drive",  "drive_create_folder",  DRIVE_CREATE_FOLDER_SCHEMA,  _handle_drive_create_folder,  "🗂️"),

    ("google_sheets", "sheets_get_values",    SHEETS_GET_VALUES_SCHEMA,    _handle_sheets_get_values,    "📊"),
    ("google_sheets", "sheets_update_values", SHEETS_UPDATE_VALUES_SCHEMA, _handle_sheets_update_values, "✏️"),
    ("google_sheets", "sheets_append_values", SHEETS_APPEND_VALUES_SCHEMA, _handle_sheets_append_values, "➕"),
    ("google_sheets", "sheets_clear",         SHEETS_CLEAR_SCHEMA,         _handle_sheets_clear,         "🧹"),
    ("google_sheets", "sheets_create",        SHEETS_CREATE_SCHEMA,        _handle_sheets_create,        "🆕"),

    ("google_docs",   "docs_get",             DOCS_GET_SCHEMA,             _handle_docs_get,             "📃"),
    ("google_docs",   "docs_insert_text",     DOCS_INSERT_TEXT_SCHEMA,     _handle_docs_insert_text,     "✍️"),
    ("google_docs",   "docs_replace_text",    DOCS_REPLACE_TEXT_SCHEMA,    _handle_docs_replace_text,    "🔁"),
    ("google_docs",   "docs_create",          DOCS_CREATE_SCHEMA,          _handle_docs_create,          "🆕"),
)


def register(ctx) -> None:
    """Register all Drive/Sheets/Docs tools. Called once by the plugin loader."""
    for toolset, name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_available,
            emoji=emoji,
        )
