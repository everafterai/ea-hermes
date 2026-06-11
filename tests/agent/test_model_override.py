"""Tests for agent/model_override.py — shared model-override entry helpers.

A "model override entry" is the shared shape used by per-channel models
(slack.channel_models), skill frontmatter overrides, cron jobs, and the
delegate tool: either a plain model string or a dict
{model, provider, base_url}.
"""

import pytest

from agent.model_override import (
    extract_skill_model_override,
    normalize_model_override,
    resolve_override_runtime,
)


# ---------------------------------------------------------------------------
# normalize_model_override
# ---------------------------------------------------------------------------


class TestNormalizeModelOverride:
    def test_plain_string(self):
        assert normalize_model_override("gpt-5-mini") == {
            "model": "gpt-5-mini",
            "provider": None,
            "base_url": None,
        }

    def test_string_is_stripped(self):
        assert normalize_model_override("  gpt-5-mini  ")["model"] == "gpt-5-mini"

    def test_blank_string_is_none(self):
        assert normalize_model_override("") is None
        assert normalize_model_override("   ") is None

    def test_none_is_none(self):
        assert normalize_model_override(None) is None

    def test_full_dict(self):
        entry = {
            "model": "claude-opus-4",
            "provider": "anthropic",
            "base_url": "https://example.com/v1",
        }
        assert normalize_model_override(entry) == entry

    def test_dict_partial_fields(self):
        out = normalize_model_override({"model": "m1"})
        assert out == {"model": "m1", "provider": None, "base_url": None}

    def test_dict_provider_only(self):
        out = normalize_model_override({"provider": "openai"})
        assert out == {"model": None, "provider": "openai", "base_url": None}

    def test_dict_blank_values_become_none(self):
        out = normalize_model_override({"model": " m ", "provider": "", "base_url": "  "})
        assert out == {"model": "m", "provider": None, "base_url": None}

    def test_dict_all_blank_is_none(self):
        assert normalize_model_override({"model": "", "provider": None}) is None
        assert normalize_model_override({}) is None

    def test_junk_types_are_none(self):
        assert normalize_model_override(42) is None
        assert normalize_model_override(["gpt-5"]) is None
        assert normalize_model_override(True) is None

    def test_dict_non_string_values_coerced(self):
        # YAML can hand back odd scalar types; coerce via str()
        out = normalize_model_override({"model": 123})
        assert out == {"model": "123", "provider": None, "base_url": None}


# ---------------------------------------------------------------------------
# extract_skill_model_override
# ---------------------------------------------------------------------------


class TestExtractSkillModelOverride:
    def test_metadata_hermes_canonical(self):
        fm = {
            "name": "x",
            "metadata": {"hermes": {"model": "gpt-5-mini", "provider": "openai"}},
        }
        out = extract_skill_model_override(fm)
        assert out == {"model": "gpt-5-mini", "provider": "openai", "base_url": None}

    def test_top_level_fallback(self):
        fm = {"name": "x", "model": "claude-opus-4"}
        out = extract_skill_model_override(fm)
        assert out == {"model": "claude-opus-4", "provider": None, "base_url": None}

    def test_metadata_hermes_wins_over_top_level(self):
        fm = {
            "model": "top-level-model",
            "metadata": {"hermes": {"model": "hermes-model"}},
        }
        assert extract_skill_model_override(fm)["model"] == "hermes-model"

    def test_field_level_no_mixing(self):
        # When metadata.hermes carries any override field, top-level is ignored
        # entirely (no per-field mixing — one source of truth per skill).
        fm = {
            "provider": "top-provider",
            "metadata": {"hermes": {"model": "hermes-model"}},
        }
        out = extract_skill_model_override(fm)
        assert out == {"model": "hermes-model", "provider": None, "base_url": None}

    def test_no_override_is_none(self):
        assert extract_skill_model_override({"name": "x"}) is None
        assert extract_skill_model_override({}) is None
        assert extract_skill_model_override({"metadata": {"hermes": {"tags": ["a"]}}}) is None

    def test_blank_values_are_none(self):
        fm = {"metadata": {"hermes": {"model": ""}}}
        assert extract_skill_model_override(fm) is None

    def test_non_dict_metadata_ignored(self):
        fm = {"metadata": "oops", "model": "m1"}
        assert extract_skill_model_override(fm)["model"] == "m1"


# ---------------------------------------------------------------------------
# resolve_override_runtime
# ---------------------------------------------------------------------------


class TestResolveOverrideRuntime:
    def test_model_only_no_provider_resolution(self, monkeypatch):
        calls = []

        def fake_resolve(**kwargs):  # pragma: no cover - must not be called
            calls.append(kwargs)
            return {}

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve
        )
        model, runtime = resolve_override_runtime(
            {"model": "gpt-5-mini", "provider": None, "base_url": None}
        )
        assert model == "gpt-5-mini"
        assert runtime == {}
        assert calls == []

    def test_provider_resolves_runtime(self, monkeypatch):
        captured = {}

        def fake_resolve(**kwargs):
            captured.update(kwargs)
            return {
                "provider": "openai",
                "api_key": "sk-test",
                "base_url": "https://api.openai.com/v1",
                "api_mode": "chat_completions",
            }

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve
        )
        model, runtime = resolve_override_runtime(
            {"model": "gpt-5-mini", "provider": "openai", "base_url": None}
        )
        assert model == "gpt-5-mini"
        assert captured["requested"] == "openai"
        assert captured["target_model"] == "gpt-5-mini"
        assert runtime["provider"] == "openai"
        assert runtime["api_key"] == "sk-test"
        assert runtime["api_mode"] == "chat_completions"

    def test_base_url_passed_explicitly(self, monkeypatch):
        captured = {}

        def fake_resolve(**kwargs):
            captured.update(kwargs)
            return {"provider": "custom", "api_key": "k", "base_url": kwargs.get("explicit_base_url"), "api_mode": "chat_completions"}

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve
        )
        model, runtime = resolve_override_runtime(
            {"model": "m", "provider": "custom:mine", "base_url": "https://self.host/v1"}
        )
        assert captured["explicit_base_url"] == "https://self.host/v1"
        assert runtime["base_url"] == "https://self.host/v1"

    def test_resolution_failure_propagates(self, monkeypatch):
        def fake_resolve(**kwargs):
            raise ValueError("no credentials for provider 'openai'")

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve
        )
        with pytest.raises(ValueError):
            resolve_override_runtime({"model": "m", "provider": "openai", "base_url": None})

    def test_provider_without_model_returns_none_model(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda **kw: {"provider": "openai", "api_key": "k", "base_url": "b", "api_mode": "chat_completions"},
        )
        model, runtime = resolve_override_runtime(
            {"model": None, "provider": "openai", "base_url": None}
        )
        assert model is None
        assert runtime["provider"] == "openai"
