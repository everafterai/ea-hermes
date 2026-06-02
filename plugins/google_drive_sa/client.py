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
# only sees files the app itself created). Override via env for least-privilege
# deployments that only need read.
_DEFAULT_SCOPES = ("https://www.googleapis.com/auth/drive",)

_LAZY_FEATURE = "plugin.google_drive_sa"

_lock = threading.Lock()
_service: Any = None
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


def get_service() -> Any:
    """Return a cached Drive v3 service, installing deps + building on first call."""
    global _service
    if _service is not None:
        return _service
    with _lock:
        if _service is not None:
            return _service
        _ensure_deps()
        from googleapiclient.discovery import build

        creds = _load_credentials()
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return _service


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
    """Drop cached service/availability (used by tests and after re-auth)."""
    global _service, _avail
    with _lock:
        _service = None
        _avail = None
