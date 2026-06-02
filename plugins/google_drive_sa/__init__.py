"""Google Drive (service account / ADC) plugin — bundled, auto-loaded.

Registers 4 tools into the ``google_drive`` toolset. Auth is the *service
account itself* via Application Default Credentials (``google.auth.default``)
— on a GCP VM that's the attached workload identity, so no key files. Share
Drive folders with the SA's email to grant the agent access.

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

_TOOLS = (
    ("drive_list_files",    DRIVE_LIST_SCHEMA,          _handle_drive_list_files,    "📁"),
    ("drive_read_file",     DRIVE_READ_SCHEMA,          _handle_drive_read_file,     "📄"),
    ("drive_upload",        DRIVE_UPLOAD_SCHEMA,        _handle_drive_upload,        "⬆️"),
    ("drive_create_folder", DRIVE_CREATE_FOLDER_SCHEMA, _handle_drive_create_folder, "🗂️"),
)


def register(ctx) -> None:
    """Register all Drive tools. Called once by the plugin loader."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="google_drive",
            schema=schema,
            handler=handler,
            check_fn=check_available,
            emoji=emoji,
        )
