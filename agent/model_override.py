"""Shared helpers for "model override entries".

A model override entry is the single shape used by every per-channel /
per-task model override surface in the fork (``slack.channel_models``,
skill frontmatter, cron jobs, the delegate tool): either a plain model
string or a mapping with optional ``model`` / ``provider`` / ``base_url``
keys — the same fields cron jobs already persist.

Pure module: no heavy imports at module level; provider credential
resolution is lazily delegated to ``hermes_cli.runtime_provider``.
"""

from typing import Any, Dict, Optional, Tuple

_OVERRIDE_FIELDS = ("model", "provider", "base_url")


def normalize_model_override(entry: Any) -> Optional[Dict[str, Optional[str]]]:
    """Normalize a model override entry into ``{model, provider, base_url}``.

    Accepts a plain model string or a dict carrying any of the override
    fields. Blank strings collapse to ``None``; an entry with no usable
    field returns ``None``. Unsupported types return ``None``.
    """
    if isinstance(entry, str):
        model = entry.strip()
        if not model:
            return None
        return {"model": model, "provider": None, "base_url": None}

    if isinstance(entry, dict):
        out: Dict[str, Optional[str]] = {}
        for field in _OVERRIDE_FIELDS:
            value = entry.get(field)
            if value is None or isinstance(value, bool):
                out[field] = None
                continue
            text = str(value).strip()
            out[field] = text or None
        if not any(out.values()):
            return None
        return out

    return None


def extract_skill_model_override(
    frontmatter: Dict[str, Any],
) -> Optional[Dict[str, Optional[str]]]:
    """Read a model override from SKILL.md frontmatter.

    ``metadata.hermes.{model,provider,base_url}`` is canonical (the
    merge-safe, agentskills.io-compatible location); top-level keys are
    accepted as a fallback. No per-field mixing between the two sources —
    when ``metadata.hermes`` carries any override field, it is the sole
    source.
    """
    if not isinstance(frontmatter, dict):
        return None

    hermes_meta: Dict[str, Any] = {}
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict):
        candidate = metadata.get("hermes")
        if isinstance(candidate, dict):
            hermes_meta = candidate

    for source in (hermes_meta, frontmatter):
        entry = {f: source.get(f) for f in _OVERRIDE_FIELDS}
        normalized = normalize_model_override(entry)
        if normalized:
            return normalized
    return None


def resolve_override_runtime(
    override: Dict[str, Optional[str]],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Resolve an override entry into ``(model, runtime_kwargs)``.

    Model-only entries return ``(model, {})`` — the caller keeps its
    current provider/credentials. When ``provider`` or ``base_url`` is
    set, credentials are resolved via
    ``hermes_cli.runtime_provider.resolve_runtime_provider`` (the cron
    pattern) and returned as runtime kwargs
    (provider/api_key/base_url/api_mode).

    Raises whatever the resolver raises on credential failure; callers
    decide fail-open vs error.
    """
    model = override.get("model")
    provider = override.get("provider")
    base_url = override.get("base_url")

    if not provider and not base_url:
        return model, {}

    import hermes_cli.runtime_provider as _rp

    runtime = _rp.resolve_runtime_provider(
        requested=provider,
        explicit_base_url=base_url,
        target_model=model,
    )
    runtime_kwargs = {
        "provider": runtime.get("provider"),
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "api_mode": runtime.get("api_mode"),
    }
    return model, runtime_kwargs
