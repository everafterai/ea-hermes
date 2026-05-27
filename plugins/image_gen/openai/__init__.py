"""OpenAI image generation backend.

Exposes OpenAI's ``gpt-image-2`` model at three quality tiers as an
:class:`ImageGenProvider` implementation. The tiers are implemented as
three virtual model IDs so the ``hermes tools`` model picker and the
``image_gen.model`` config key behave like any other multi-model backend:

    gpt-image-2-low     ~15s   fastest, good for iteration
    gpt-image-2-medium  ~40s   default — balanced
    gpt-image-2-high    ~2min  slowest, highest fidelity

All three hit the same underlying API model (``gpt-image-2``) with a
different ``quality`` parameter. Output is base64 JSON → saved under
``$HERMES_HOME/cache/images/``.

Selection precedence (first hit wins):

1. ``OPENAI_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.openai.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it's one of our tier IDs)
4. :data:`DEFAULT_MODEL` — ``gpt-image-2-medium``
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
#
# All three IDs resolve to the same underlying API model with a different
# ``quality`` setting. ``api_model`` is what gets sent to OpenAI;
# ``quality`` is the knob that changes generation time and output fidelity.

API_MODEL = "gpt-image-2"

_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2-low": {
        "display": "GPT Image 2 (Low)",
        "speed": "~15s",
        "strengths": "Fast iteration, lowest cost",
        "quality": "low",
    },
    "gpt-image-2-medium": {
        "display": "GPT Image 2 (Medium)",
        "speed": "~40s",
        "strengths": "Balanced — default",
        "quality": "medium",
    },
    "gpt-image-2-high": {
        "display": "GPT Image 2 (High)",
        "speed": "~2min",
        "strengths": "Highest fidelity, strongest prompt adherence",
        "quality": "high",
    },
}

DEFAULT_MODEL = "gpt-image-2-medium"

_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}


def _load_openai_config() -> Dict[str, Any]:
    """Read ``image_gen`` from config.yaml (returns {} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Decide which tier to use and return ``(model_id, meta)``."""
    env_override = os.environ.get("OPENAI_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_openai_config()
    openai_cfg = cfg.get("openai") if isinstance(cfg.get("openai"), dict) else {}
    candidate: Optional[str] = None
    if isinstance(openai_cfg, dict):
        value = openai_cfg.get("model")
        if isinstance(value, str) and value in _MODELS:
            candidate = value
    if candidate is None:
        top = cfg.get("model")
        if isinstance(top, str) and top in _MODELS:
            candidate = top

    if candidate is not None:
        return candidate, _MODELS[candidate]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


# ---------------------------------------------------------------------------
# reference_images normalization (image-to-image input handling)
# ---------------------------------------------------------------------------

_MAX_REF_BYTES = 25 * 1024 * 1024  # matches save_url_image's cap

_DATA_URI_RE = re.compile(
    r"^data:image/(?P<ext>png|jpe?g|webp|gif);base64,(?P<payload>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_references(refs: List[str]) -> List[Tuple[BinaryIO, str]]:
    """Resolve path/URL/data-URI strings to open binary file handles.

    Returns ``list[(handle, filename)]`` — caller is responsible for closing
    the handles. Raises :class:`ValueError` prefixed with
    ``reference_images[<index>]: ...`` on any bad entry. Handles opened
    before the failing entry are closed before the exception propagates.

    Accepted input shapes per entry:
      * ``data:image/<ext>;base64,<payload>`` — decoded and written to
        ``$HERMES_HOME/cache/images/openai_ref_<ts>_<uuid>.<ext>``.
      * ``http://`` / ``https://`` URL — fetched via
        :func:`save_url_image` (same 25MB cap).
      * Anything else — treated as a local filesystem path; must exist,
        be a regular file, and be ≤ 25 MB.
    """
    opened: List[Tuple[BinaryIO, str]] = []

    def _cleanup_and_raise(idx: int, msg: str) -> None:
        for h, _ in opened:
            try:
                h.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        raise ValueError(f"reference_images[{idx}]: {msg}")

    for idx, raw in enumerate(refs):
        if not isinstance(raw, str) or not raw.strip():
            _cleanup_and_raise(idx, "must be a non-empty string")

        value = raw.strip()

        # ── data: URI ────────────────────────────────────────────────
        if value.startswith("data:"):
            match = _DATA_URI_RE.match(value)
            if not match:
                _cleanup_and_raise(idx, "malformed data URI")
            ext = match.group("ext").lower()
            if ext == "jpeg":
                ext = "jpg"
            try:
                payload_bytes = base64.b64decode(
                    match.group("payload"), validate=True
                )
            except (binascii.Error, ValueError):
                _cleanup_and_raise(idx, "malformed data URI (invalid base64)")
            if len(payload_bytes) > _MAX_REF_BYTES:
                _cleanup_and_raise(
                    idx,
                    f"data URI exceeds {_MAX_REF_BYTES // (1024 * 1024)}MB limit",
                )
            saved = save_b64_image(
                match.group("payload"),
                prefix="openai_ref",
                extension=ext,
            )
            opened.append((open(saved, "rb"), saved.name))
            continue

        # ── http(s) URL ──────────────────────────────────────────────
        if value.startswith(("http://", "https://")):
            try:
                saved = save_url_image(value, prefix="openai_ref")
            except Exception as exc:  # noqa: BLE001
                _cleanup_and_raise(idx, f"fetch failed: {exc}")
            opened.append((open(saved, "rb"), saved.name))
            continue

        # ── local path ──────────────────────────────────────────────
        path = Path(value).expanduser()
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            _cleanup_and_raise(idx, f"could not resolve path ({exc})")
        if not path.exists() or not path.is_file():
            _cleanup_and_raise(idx, f"{path} not found")
        size = path.stat().st_size
        if size > _MAX_REF_BYTES:
            _cleanup_and_raise(
                idx,
                f"{path} exceeds {_MAX_REF_BYTES // (1024 * 1024)}MB limit",
            )
        opened.append((open(path, "rb"), path.name))

    return opened


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAIImageGenProvider(ImageGenProvider):
    """OpenAI ``images.generate`` backend — gpt-image-2 at low/medium/high."""

    @property
    def name(self) -> str:
        return "openai"

    @property
    def display_name(self) -> str:
        return "OpenAI"

    def is_available(self) -> bool:
        if not os.environ.get("OPENAI_API_KEY"):
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "varies",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "OpenAI",
            "badge": "paid",
            "tag": "gpt-image-2 at low/medium/high quality tiers",
            "env_vars": [
                {
                    "key": "OPENAI_API_KEY",
                    "prompt": "OpenAI API key",
                    "url": "https://platform.openai.com/api-keys",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="openai",
                aspect_ratio=aspect,
            )

        if not os.environ.get("OPENAI_API_KEY"):
            return error_response(
                error=(
                    "OPENAI_API_KEY not set. Run `hermes tools` → Image "
                    "Generation → OpenAI to configure, or `hermes setup` "
                    "to add the key."
                ),
                error_type="auth_required",
                provider="openai",
                aspect_ratio=aspect,
            )

        try:
            import openai
        except ImportError:
            return error_response(
                error="openai Python package not installed (pip install openai)",
                error_type="missing_dependency",
                provider="openai",
                aspect_ratio=aspect,
            )

        tier_id, meta = _resolve_model()
        size = _SIZES.get(aspect, _SIZES["square"])

        # gpt-image-2 returns b64_json unconditionally and REJECTS
        # ``response_format`` as an unknown parameter. Don't send it.
        payload: Dict[str, Any] = {
            "model": API_MODEL,
            "prompt": prompt,
            "size": size,
            "n": 1,
            "quality": meta["quality"],
        }

        try:
            client = openai.OpenAI()
            response = client.images.generate(**payload)
        except Exception as exc:
            logger.debug("OpenAI image generation failed", exc_info=True)
            return error_response(
                error=f"OpenAI image generation failed: {exc}",
                error_type="api_error",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        data = getattr(response, "data", None) or []
        if not data:
            return error_response(
                error="OpenAI returned no image data",
                error_type="empty_response",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0]
        b64 = getattr(first, "b64_json", None)
        url = getattr(first, "url", None)
        revised_prompt = getattr(first, "revised_prompt", None)

        if b64:
            try:
                saved_path = save_b64_image(b64, prefix=f"openai_{tier_id}")
            except Exception as exc:
                return error_response(
                    error=f"Could not save image to cache: {exc}",
                    error_type="io_error",
                    provider="openai",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        elif url:
            # Defensive — gpt-image-2 returns b64 today, but OpenAI's API
            # has previously returned URLs.  Cache the bytes locally so the
            # gateway never tries to fetch an ephemeral / signed URL after
            # it expires — same rationale as the xAI provider (#26942).
            try:
                saved_path = save_url_image(url, prefix=f"openai_{tier_id}")
            except Exception as exc:
                logger.warning(
                    "OpenAI image URL %s could not be cached (%s); falling back to bare URL.",
                    url,
                    exc,
                )
                image_ref = url
            else:
                image_ref = str(saved_path)
        else:
            return error_response(
                error="OpenAI response contained neither b64_json nor URL",
                error_type="empty_response",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"size": size, "quality": meta["quality"]}
        if revised_prompt:
            extra["revised_prompt"] = revised_prompt

        return success_response(
            image=image_ref,
            model=tier_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="openai",
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``OpenAIImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(OpenAIImageGenProvider())
