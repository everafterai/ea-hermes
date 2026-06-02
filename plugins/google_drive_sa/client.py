"""Google Drive client backed by a service account via ADC.

Credential resolution mirrors the Google Chat adapter
(``plugins/platforms/google_chat/adapter.py``) so the gateway can run on a
GCP VM / Cloud Run with an *attached* service account and zero key files:

  1. Explicit ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` env (path or inline JSON)
  2. ``GOOGLE_APPLICATION_CREDENTIALS`` env (path)
  3. Application Default Credentials via ``google.auth.default()`` — picks up
     the VM's workload identity automatically (the everafter-deployed path),
     or ``gcloud auth application-default login`` locally.

The agent acts as the *service account itself* (no domain-wide delegation):
share Drive files/folders with the SA's email to grant access. This keeps the
blast radius to exactly what's shared.

Heavy google-* imports are lazy so the plugin can register its tools at
startup without the deps installed; first real use triggers a venv-scoped
install via :mod:`tools.lazy_deps`.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

# Full-drive scope is required to see folders *shared with* the SA (drive.file
# only sees files the app itself created). Sheets/Docs structured editing each
# need their own scope. Override via env for least-privilege deployments.
# NOTE: scopes are only *requested* on the token — actual access still comes
# from sharing the file with the SA's email (Editor for writes).
_DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
)

_LAZY_FEATURE = "plugin.google_drive_sa"

_lock = threading.Lock()
_services: dict[tuple[str, str], Any] = {}
_avail: bool | None = None


def _scopes() -> list[str]:
    raw = os.getenv("HERMES_GDRIVE_SCOPES", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return list(_DEFAULT_SCOPES)


def _ensure_deps(*, prompt: bool = False) -> None:
    """Install google-api-python-client / google-auth on first use."""
    from tools import lazy_deps

    lazy_deps.ensure(_LAZY_FEATURE, prompt=prompt)


def _load_credentials() -> Any:
    """Resolve service-account credentials (see module docstring for order)."""
    scopes = _scopes()
    sa = (
        os.getenv("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )
    if sa:
        from google.oauth2 import service_account

        if sa.lstrip().startswith("{"):
            try:
                info = json.loads(sa)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Inline SA JSON is not valid JSON: {exc}") from exc
            return service_account.Credentials.from_service_account_info(
                info, scopes=scopes
            )
        if not os.path.exists(sa):
            raise FileNotFoundError(
                f"Service Account JSON file not found at configured path: {sa}"
            )
        return service_account.Credentials.from_service_account_file(sa, scopes=scopes)

    # No explicit key — Application Default Credentials (the VM-attached SA).
    import google.auth

    creds, _project = google.auth.default(scopes=scopes)
    return creds


def _get_service(api: str, version: str) -> Any:
    """Return a cached googleapiclient service, installing deps + building once."""
    key = (api, version)
    svc = _services.get(key)
    if svc is not None:
        return svc
    with _lock:
        svc = _services.get(key)
        if svc is not None:
            return svc
        _ensure_deps()
        from googleapiclient.discovery import build

        creds = _load_credentials()
        svc = build(api, version, credentials=creds, cache_discovery=False)
        _services[key] = svc
        return svc


def get_service() -> Any:
    """Cached Drive v3 service."""
    return _get_service("drive", "v3")


def get_sheets_service() -> Any:
    """Cached Sheets v4 service."""
    return _get_service("sheets", "v4")


def get_docs_service() -> Any:
    """Cached Docs v1 service."""
    return _get_service("docs", "v1")


def check_available() -> bool:
    """Cheap gate for the tool registry — True when ADC is resolvable.

    Never installs and never raises. If the google-* deps aren't importable
    yet we optimistically return True so the toolset can be enabled and the
    deps install on first dispatch; the handler surfaces any real ADC error.
    """
    global _avail
    if _avail is not None:
        return _avail
    try:
        import google.auth  # noqa: F401
    except Exception:
        # Deps not installed yet — let the user enable + use it (handler installs).
        return True
    try:
        _load_credentials()
        _avail = True
    except Exception:
        _avail = False
    return _avail


def reset_cache() -> None:
    """Drop cached services/availability (used by tests and after re-auth)."""
    global _avail
    with _lock:
        _services.clear()
        _avail = None
