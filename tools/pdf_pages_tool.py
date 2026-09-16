"""pdf_pages — render pages of a local PDF to PNG so a vision model can read them.

``read_file`` (and the fork's ``drive_read_file``) extract a PDF's text layer.
A scanned statement has no text layer, and the extractor's own ``NEEDS OCR``
warning tells the model to "render the pages with pdftoppm and inspect via
vision_analyze" — advice that assumes a shell. Upstream's shell-free answer is
hosted OCR (Firecrawl, a key, a per-page bill). Ours is the model's own eyes:
the agents already run on vision-capable models, and they read a Hebrew bank
statement better than tesseract would.

This tool is the one missing step. It takes a local PDF path and a page
selection, runs poppler's ``pdfinfo``/``pdftoppm`` with a fixed argv (never a
shell string), and writes PNGs into ``$HERMES_HOME/cache/images/`` — where
``vision_analyze`` already reads from. Same shape and same reasoning as
``video_frames``: pulling images out of a user's file is not implied by
``vision`` or ``file``, and it must not require ``terminal``.

Registered as its OWN toolset (``pdf_pages``) so RBAC gates it independently.
Requires ``pdftoppm``/``pdfinfo`` on the host — ``check_fn`` hides the tool when
they are absent, so the model is never offered a capability it cannot use.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from agent.file_safety import raise_if_read_blocked
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Every rendered page becomes an image the model may then look at. Bound both
# the work and the context blast radius; a 200-page export is paged through in
# several calls, not one.
_MAX_PAGES = 20

# 150 dpi renders a Letter/A4 page at ~1275×1650 — plenty for a vision model to
# read 9-pt statement text, and small enough to keep per-page image tokens sane.
_DEFAULT_DPI = 150
_MIN_DPI = 50
_MAX_DPI = 300

_PROBE_TIMEOUT_SECONDS = 30
_RENDER_TIMEOUT_SECONDS = 120


class PageRenderError(RuntimeError):
    """poppler could not produce the requested page."""


class PopplerMissingError(PageRenderError):
    """``pdftoppm``/``pdfinfo`` is not on this host at all — abandon the call."""


_POPPLER_MISSING_HINT = (
    "poppler (pdftoppm/pdfinfo) is not installed on this host, so PDF pages "
    "cannot be rendered. An operator needs to install it once (e.g. "
    "`sudo apt-get install -y poppler-utils`)."
)


def _pages_output_dir() -> Path:
    """Return ``$HERMES_HOME/cache/images/`` — the hand-off point every image
    consumer (vision_analyze, webflow_asset_upload) already reads from."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ─── poppler legs ────────────────────────────────────────────────────────────


async def _run_argv(argv: list[str], *, timeout: int) -> tuple[int, bytes, bytes]:
    """Run a fixed argv with no shell and return ``(rc, stdout, stderr)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise PopplerMissingError(_POPPLER_MISSING_HINT) from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # pragma: no cover - process already gone
            pass
        raise PageRenderError(f"{argv[0]} timed out after {timeout}s") from None
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout or b"", stderr or b""


_PAGES_LINE = re.compile(r"^Pages:\s+(\d+)\s*$", re.M)


async def _probe_page_count(pdf_path: Path) -> int:
    """Return the page count via ``pdfinfo``."""
    rc, stdout, stderr = await _run_argv(
        ["pdfinfo", str(pdf_path)], timeout=_PROBE_TIMEOUT_SECONDS
    )
    text = stdout.decode("utf-8", "replace")
    match = _PAGES_LINE.search(text)
    if rc != 0 or not match:
        detail = stderr.decode("utf-8", "replace").strip()[:300] or "no page count reported"
        raise PageRenderError(f"Could not read the PDF: {detail}")
    count = int(match.group(1))
    if count <= 0:
        raise PageRenderError("PDF reports zero pages.")
    return count


async def _render_page(pdf_path: Path, page: int, out_path: Path, dpi: int) -> None:
    """Write page *page* (1-based) as a PNG to *out_path*.

    ``-singlefile`` makes pdftoppm write exactly ``<prefix>.png`` instead of a
    zero-padded ``<prefix>-N.png`` whose padding depends on the document's page
    count — so the output name is known before the call, like ffmpeg's.
    """
    prefix = str(out_path.with_suffix(""))
    argv = [
        "pdftoppm",
        "-png",
        "-r", str(dpi),
        "-f", str(page),
        "-l", str(page),
        "-singlefile",
        str(pdf_path),
        prefix,
    ]
    rc, _stdout, stderr = await _run_argv(argv, timeout=_RENDER_TIMEOUT_SECONDS)
    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        detail = stderr.decode("utf-8", "replace").strip()[:300] or f"exit code {rc}"
        raise PageRenderError(f"pdftoppm: {detail}")


# ─── argument validation ─────────────────────────────────────────────────────


def _resolve_source(raw_path: str) -> Path:
    """Validate the model-supplied PDF path. Raises ``ValueError``."""
    text = (raw_path or "").strip()
    if not text:
        raise ValueError("pdf_path is required.")
    if text.startswith("file://"):
        text = text[len("file://"):]
    path = Path(os.path.expanduser(text)).resolve()

    # Order matters: the read guard runs before anything reports whether the
    # path exists, so this tool can't be used to probe for other users' files.
    raise_if_read_blocked(str(path))

    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Unsupported file type '{path.suffix or path.name}'. Only .pdf is accepted.")
    if not path.is_file():
        raise ValueError(f"PDF not found: {path}")
    return path


_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def _resolve_pages(raw) -> list[int] | None:
    """Parse ``"1-3"``, ``"2,5"``, ``"1-3,7"`` into sorted unique 1-based pages.

    ``None`` (omitted) means every page. Raises ``ValueError`` naming the
    offending piece.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        raise ValueError('pages must be like "1-3", "2,5" or "1-3,7" (omit for all pages).')
    pages: set[int] = set()
    for piece in text.split(","):
        piece = piece.strip()
        match = _RANGE_RE.match(piece)
        if not match:
            raise ValueError(f"Invalid page selection {piece!r}. Use \"1-3\", \"2,5\" or \"1-3,7\".")
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first < 1 or last < first:
            raise ValueError(f"Invalid page range {piece!r}: pages are 1-based and ranges ascend.")
        pages.update(range(first, last + 1))
    return sorted(pages)


def _resolve_dpi(raw) -> int:
    if raw is None:
        return _DEFAULT_DPI
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"dpi must be a number between {_MIN_DPI} and {_MAX_DPI}.")
    dpi = int(raw)
    if not _MIN_DPI <= dpi <= _MAX_DPI:
        raise ValueError(f"dpi {dpi} is out of range ({_MIN_DPI}–{_MAX_DPI}).")
    return dpi


# ─── tool ────────────────────────────────────────────────────────────────────

PDF_PAGES_SCHEMA = {
    "name": "pdf_pages",
    "description": (
        "Render pages of a LOCAL PDF to PNG images and get their file paths "
        "back, so you can READ them with vision_analyze. Use this when a PDF's "
        "text extraction came back empty or with a NEEDS OCR warning (a "
        "scanned statement, a photographed form, a figure-only page), or when "
        "the layout matters (tables, stamps, signatures). Pass the local path "
        "— e.g. the `local_path` drive_read_file returned, or where an "
        "attachment was saved. Omit `pages` for the whole document (first "
        f"{_MAX_PAGES} pages), or give a selection to keep the image budget "
        "small. Feed each returned path to vision_analyze. This does NOT "
        "extract text itself — read_file does that when a text layer exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pdf_path": {
                "type": "string",
                "description": "Absolute path to the local PDF file.",
            },
            "pages": {
                "type": "string",
                "description": (
                    "Optional. Which pages to render, 1-based: \"1-3\", "
                    "\"2,5\" or \"1-3,7\". Omit for all pages (capped at "
                    f"{_MAX_PAGES} per call)."
                ),
            },
            "dpi": {
                "type": "integer",
                "description": (
                    f"Optional. Render resolution, {_MIN_DPI}–{_MAX_DPI}. "
                    f"Default {_DEFAULT_DPI} — readable body text at sane "
                    "image size; raise it only for tiny print."
                ),
            },
        },
        "required": ["pdf_path"],
    },
}


async def _pdf_pages_handler(args: dict, **_kw) -> str:
    try:
        source = _resolve_source(args.get("pdf_path", ""))
        wanted = _resolve_pages(args.get("pages"))
        dpi = _resolve_dpi(args.get("dpi"))
    except ValueError as e:
        return tool_error(str(e))

    try:
        page_count = await _probe_page_count(source)
    except PageRenderError as e:
        return tool_error(str(e))

    note = None
    if wanted is None:
        if page_count > _MAX_PAGES:
            note = (
                f"Document has {page_count} pages; rendered the first "
                f"{_MAX_PAGES}. Call again with pages=\"{_MAX_PAGES + 1}-…\" for the rest."
            )
        wanted = list(range(1, min(page_count, _MAX_PAGES) + 1))
    elif len(wanted) > _MAX_PAGES:
        note = f"Selection had {len(wanted)} pages; rendered the first {_MAX_PAGES}."
        wanted = wanted[:_MAX_PAGES]

    out_dir = _pages_output_dir()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    batch = uuid.uuid4().hex[:8]

    pages: list[dict] = []
    skipped: list[dict] = []
    failures: list[str] = []

    for page in wanted:
        if page > page_count:
            skipped.append({
                "page": page,
                "reason": f"page {page} does not exist (document has {page_count} pages).",
            })
            continue

        out_path = out_dir / f"page_{stamp}_{batch}_{page:03d}.png"
        try:
            await _render_page(source, page, out_path, dpi)
        except PopplerMissingError as e:
            return tool_error(str(e))
        except PageRenderError as e:
            logger.warning("[pdf_pages] page %s failed: %s", page, e)
            failures.append(f"page {page}: {e}")
            continue

        pages.append({
            "page": page,
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
        })

    if not pages:
        detail = "; ".join(failures or [s["reason"] for s in skipped])
        return tool_error(f"No pages could be rendered from {source.name}. {detail}")

    payload = {
        "success": True,
        "pdf_path": str(source),
        "page_count": page_count,
        "rendered": len(pages),
        "dpi": dpi,
        "pages": pages,
    }
    if note:
        payload["note"] = note
    if skipped:
        payload["skipped"] = skipped
    if failures:
        payload["failures"] = failures
    return tool_result(payload)


def _check_poppler_available() -> bool:
    """Hide the tool when the host has no poppler, rather than failing per call."""
    return bool(shutil.which("pdftoppm")) and bool(shutil.which("pdfinfo"))


registry.register(
    name="pdf_pages",
    toolset="pdf_pages",
    schema=PDF_PAGES_SCHEMA,
    handler=lambda args, **kw: _pdf_pages_handler(args, **kw),
    check_fn=_check_poppler_available,
    requires_env=[],
    is_async=True,
    emoji="📄",
)
