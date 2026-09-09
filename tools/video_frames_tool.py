"""video_frames — extract still frames from a local video file.

The ``video`` toolset's ``video_analyze`` sends a whole video to a multimodal
model and returns *prose*. That answers "what happens in this clip"; it cannot
answer "give me the screenshot at 0:42" because it never writes an image. So an
agent handed a product-demo MP4 in Slack — the frames of which are meant to
become in-article visuals — has no route to a PNG except shelling out to
ffmpeg, which means granting ``terminal``: a host shell, for a screenshot.

This tool is that one missing step. It takes a local video path plus either
explicit timestamps or a frame count, runs ffmpeg with a fixed argv (never a
shell string), and writes PNGs into ``$HERMES_HOME/cache/images/`` — where
``vision_analyze`` can QA them and ``webflow_asset_upload`` can push them to the
CDN. The pipeline either side of it already worked; only the extraction was
missing.

Registered as its OWN toolset (``video_frames``) so RBAC gates it
independently: pulling frames out of a user's upload is not implied by
``vision`` (analysing an image you were given) or by ``file``, and it must not
require ``terminal``. Least privilege is the entire reason this file exists
rather than a `hermes tools enable terminal`.

Requires ``ffmpeg``/``ffprobe`` on the host — ``check_fn`` hides the tool when
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

# Mirrors gateway.platforms.base.SUPPORTED_VIDEO_TYPES (the set the Slack/
# Telegram/etc. adapters actually cache to disk) plus the containers ffmpeg
# handles trivially. Duplicated rather than imported: a tool must not pull the
# gateway package in, and this list is an input allowlist, not shared state.
_SUPPORTED_EXTENSIONS = {
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg",
}

# A frame grab is cheap but not free, and every frame becomes an image the
# model may then look at. Bound both the work and the context blast radius.
_MAX_FRAMES = 24
_DEFAULT_FRAME_COUNT = 6

_MIN_WIDTH = 64
_MAX_WIDTH = 4096

_PROBE_TIMEOUT_SECONDS = 30
_EXTRACT_TIMEOUT_SECONDS = 120


class FrameExtractionError(RuntimeError):
    """ffmpeg could not produce a frame at the requested position."""


class FfmpegMissingError(FrameExtractionError):
    """The ffmpeg/ffprobe binary is not on this host at all.

    Distinct from a per-frame failure: no later timestamp is going to work
    either, so the handler abandons the whole call instead of retrying 23 more
    times and reporting 23 identical failures.
    """


_FFMPEG_MISSING_HINT = (
    "ffmpeg/ffprobe is not installed on this host, so frames cannot be "
    "extracted. An operator needs to install it once (e.g. "
    "`sudo apt-get install -y ffmpeg`)."
)


def _frames_output_dir() -> Path:
    """Return ``$HERMES_HOME/cache/images/``, creating parents as needed.

    Frames land beside generated images on purpose: that directory is already
    the hand-off point every image consumer knows how to read from.
    """
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ─── timestamp parsing ───────────────────────────────────────────────────────

_CLOCK_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def _parse_timestamp(value) -> float:
    """Parse ``12``, ``12.5``, ``"1:05"``, ``"1:05.5"`` or ``"01:02:03"``.

    Raises ``ValueError`` with the offending value so the model gets told what
    it wrote, not just that something was wrong.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a timestamp
        raise ValueError(f"Invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        match = _CLOCK_RE.match(text)
        if not match:
            raise ValueError(
                f"Invalid timestamp: {value!r}. Use seconds (12, 12.5) or "
                f"clock form (\"1:05\", \"01:02:03\")."
            )
        first, second, rest = match.groups()
        parts = [p for p in (first, second, rest) if p is not None]
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    if seconds < 0:
        raise ValueError(f"Invalid timestamp: {value!r} is negative.")
    return seconds


def _format_timestamp(seconds: float) -> str:
    """Render seconds as ``H:MM:SS.s`` / ``M:SS.s`` for the result payload."""
    whole = int(seconds)
    frac = seconds - whole
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    tail = f"{secs:02d}" + (f".{round(frac * 10)}" if frac >= 0.05 else "")
    if hours:
        return f"{hours}:{minutes:02d}:{tail}"
    return f"{minutes}:{tail}"


def _evenly_spaced(duration: float, count: int) -> list[float]:
    """Return *count* sample points spread across *duration*.

    Offset by half a step at each end: the first and last frames of a screen
    recording are usually a black fade or a still title card, which is exactly
    the frame nobody wants.
    """
    step = duration / count
    return [round(step * (i + 0.5), 3) for i in range(count)]


# ─── ffmpeg legs ─────────────────────────────────────────────────────────────


async def _run_argv(argv: list[str], *, timeout: int) -> tuple[int, bytes, bytes]:
    """Run a fixed argv with no shell and return ``(rc, stdout, stderr)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        # Only exec itself raises this; a missing *output* directory would
        # surface later as a non-zero exit, so this is unambiguous.
        raise FfmpegMissingError(_FFMPEG_MISSING_HINT) from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # pragma: no cover - process already gone
            pass
        raise FrameExtractionError(
            f"{argv[0]} timed out after {timeout}s"
        ) from None
    # communicate() has reaped the process, so returncode is set; None would
    # mean "still running", which we treat as a failure rather than a success.
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout or b"", stderr or b""


async def _probe_duration(video_path: Path) -> float:
    """Return the video's duration in seconds via ffprobe."""
    rc, stdout, stderr = await _run_argv(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    text = stdout.decode("utf-8", "replace").strip()
    if rc != 0 or not text:
        detail = stderr.decode("utf-8", "replace").strip()[:300] or "no duration reported"
        raise FrameExtractionError(f"Could not read video duration: {detail}")
    try:
        duration = float(text.splitlines()[0])
    except ValueError:
        raise FrameExtractionError(
            f"Could not read video duration: ffprobe returned {text[:80]!r}"
        ) from None
    if duration <= 0:
        raise FrameExtractionError("Video reports a zero-length duration.")
    return duration


async def _extract_frame(
    video_path: Path,
    seconds: float,
    out_path: Path,
    max_width: int | None,
) -> None:
    """Write a single PNG frame at *seconds* to *out_path*.

    ``-ss`` before ``-i`` is the fast seek: ffmpeg jumps to the preceding
    keyframe and decodes forward, which for a screen recording is both accurate
    enough and orders of magnitude cheaper than decoding from zero.
    """
    argv = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-ss", f"{seconds:g}",
        "-i", str(video_path),
        "-frames:v", "1",
    ]
    if max_width:
        # -2 keeps the aspect ratio and an even height (required by some
        # encoders); scale down only, never upscale a low-res source.
        argv += ["-vf", f"scale='min({max_width},iw)':-2"]
    argv += ["-y", str(out_path)]

    rc, _stdout, stderr = await _run_argv(argv, timeout=_EXTRACT_TIMEOUT_SECONDS)
    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        detail = stderr.decode("utf-8", "replace").strip()[:300] or f"exit code {rc}"
        raise FrameExtractionError(f"ffmpeg: {detail}")


# ─── argument validation ─────────────────────────────────────────────────────


def _resolve_source(raw_path: str) -> Path:
    """Validate the model-supplied video path. Raises ``ValueError``."""
    text = (raw_path or "").strip()
    if not text:
        raise ValueError("video_path is required.")
    if text.startswith("file://"):
        text = text[len("file://"):]
    path = Path(os.path.expanduser(text)).resolve()

    # Order matters: the read guard runs before anything reports whether the
    # path exists, so this tool can't be used to probe for other users' files.
    raise_if_read_blocked(str(path))

    if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported video type '{path.suffix or path.name}'. Supported: "
            f"{', '.join(sorted(_SUPPORTED_EXTENSIONS))}."
        )
    if not path.is_file():
        raise ValueError(f"Video file not found: {path}")
    return path


def _resolve_max_width(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        width = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"max_width must be a whole number, got {raw!r}.") from None
    if not (_MIN_WIDTH <= width <= _MAX_WIDTH):
        raise ValueError(
            f"max_width must be between {_MIN_WIDTH} and {_MAX_WIDTH}, got {width}."
        )
    return width


def _resolve_timestamps(raw) -> list[float] | None:
    """Parse the explicit timestamp list, or return None to sample evenly."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError("timestamps must be a list.")
    if not raw:
        return None
    if len(raw) > _MAX_FRAMES:
        raise ValueError(
            f"Too many timestamps ({len(raw)}); at most {_MAX_FRAMES} frames "
            f"per call."
        )
    return [_parse_timestamp(value) for value in raw]


def _resolve_count(raw) -> int:
    if raw is None or raw == "":
        return _DEFAULT_FRAME_COUNT
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"count must be a whole number, got {raw!r}.") from None
    # Clamp rather than reject: an over-eager count is a harmless mistake, and
    # failing the call would cost a round trip to learn the ceiling.
    return max(1, min(count, _MAX_FRAMES))


# ─── handler ─────────────────────────────────────────────────────────────────


VIDEO_FRAMES_SCHEMA = {
    "name": "video_frames",
    "description": (
        "Extract still frames (PNG screenshots) from a LOCAL video file and get "
        "their file paths back. Use this whenever you need images OUT of a "
        "video — screenshots for an article, a thumbnail, or frames to inspect "
        "with vision_analyze. Pass the local path the user's video attachment "
        "was saved to. Give explicit timestamps when you know which moments "
        "matter, otherwise ask for a count and it samples evenly across the "
        "whole video. The returned paths can be fed straight to vision_analyze "
        "or webflow_asset_upload. This does NOT describe the video — use "
        "video_analyze for that."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video_path": {
                "type": "string",
                "description": (
                    "Absolute path to the local video file (mp4, mov, webm, "
                    "mkv, avi). Platform attachments are cached under "
                    "$HERMES_HOME/cache/videos/."
                ),
            },
            "timestamps": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional. Exact positions to grab, as seconds (\"12\", "
                    "\"12.5\") or clock form (\"1:05\", \"01:02:03\"). Omit to "
                    "sample evenly instead."
                ),
            },
            "count": {
                "type": "integer",
                "description": (
                    f"Optional. How many frames to sample evenly across the "
                    f"video when no timestamps are given. Default "
                    f"{_DEFAULT_FRAME_COUNT}, max {_MAX_FRAMES}."
                ),
            },
            "max_width": {
                "type": "integer",
                "description": (
                    "Optional. Scale frames down to at most this width in "
                    "pixels, preserving aspect ratio. Never upscales."
                ),
            },
        },
        "required": ["video_path"],
    },
}


async def _video_frames_handler(args: dict, **_kw) -> str:
    try:
        source = _resolve_source(args.get("video_path", ""))
        max_width = _resolve_max_width(args.get("max_width"))
        timestamps = _resolve_timestamps(args.get("timestamps"))
        count = _resolve_count(args.get("count"))
    except ValueError as e:
        return tool_error(str(e))

    try:
        duration = await _probe_duration(source)
    except FrameExtractionError as e:
        return tool_error(str(e))

    if timestamps is None:
        timestamps = _evenly_spaced(duration, count)

    out_dir = _frames_output_dir()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    batch = uuid.uuid4().hex[:8]

    frames: list[dict] = []
    skipped: list[dict] = []
    failures: list[str] = []

    for index, seconds in enumerate(timestamps):
        if seconds >= duration:
            skipped.append({
                "timestamp_seconds": seconds,
                "reason": (
                    f"{seconds:g}s is past the end of the video "
                    f"({duration:g}s long)."
                ),
            })
            continue

        out_path = out_dir / f"frame_{stamp}_{batch}_{index:02d}.png"
        try:
            await _extract_frame(source, seconds, out_path, max_width)
        except FfmpegMissingError as e:
            return tool_error(str(e))
        except FrameExtractionError as e:
            logger.warning(
                "[video_frames] frame at %ss failed: %s", seconds, e
            )
            failures.append(f"{seconds:g}s: {e}")
            continue

        frames.append({
            "index": index,
            "timestamp_seconds": seconds,
            "timestamp": _format_timestamp(seconds),
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
        })

    if not frames:
        detail = "; ".join(failures or [s["reason"] for s in skipped])
        return tool_error(
            f"No frames could be extracted from {source.name}. {detail}"
        )

    payload = {
        "success": True,
        "video_path": str(source),
        "duration_seconds": round(duration, 3),
        "frame_count": len(frames),
        "frames": frames,
    }
    if skipped:
        payload["skipped"] = skipped
    if failures:
        payload["failures"] = failures
    return tool_result(payload)


def _check_ffmpeg_available() -> bool:
    """Hide the tool when the host has no ffmpeg, rather than failing per call."""
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


registry.register(
    name="video_frames",
    toolset="video_frames",
    schema=VIDEO_FRAMES_SCHEMA,
    handler=lambda args, **kw: _video_frames_handler(args, **kw),
    check_fn=_check_ffmpeg_available,
    requires_env=[],
    is_async=True,
    emoji="🎞️",
)
