"""Tests for skill-declared model overrides in the gateway.

A skill whose frontmatter carries metadata.hermes.{model,provider,base_url}
switches the session's model for the rest of the session, exactly like the
/model command: the override bundle is written to _session_model_overrides,
the cached agent is evicted, and a pending model note is queued.
"""

import threading

import pytest

import gateway.run as gateway_run


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    return runner


def _seed_cache(runner, session_key):
    runner._agent_cache[session_key] = (object(), "sig")


class TestApplySkillModelOverride:
    def test_model_only_override_applied(self):
        runner = _make_runner()
        _seed_cache(runner, "k1")
        runner._apply_skill_model_override(
            "k1", {"model": "gpt-5-mini", "provider": None, "base_url": None}, "fancy"
        )
        assert runner._session_model_overrides["k1"]["model"] == "gpt-5-mini"
        assert "k1" not in runner._agent_cache  # evicted
        note = runner._pending_model_notes["k1"]
        assert "fancy" in note and "gpt-5-mini" in note

    def test_provider_override_resolves_bundle(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda **kw: {
                "provider": "openai",
                "api_key": "sk-skill",
                "base_url": "https://api.openai.com/v1",
                "api_mode": "chat_completions",
            },
        )
        runner = _make_runner()
        runner._apply_skill_model_override(
            "k1", {"model": "gpt-5-mini", "provider": "openai", "base_url": None}, "fancy"
        )
        stored = runner._session_model_overrides["k1"]
        assert stored["api_key"] == "sk-skill"
        assert stored["provider"] == "openai"
        assert stored["api_mode"] == "chat_completions"

    def test_repeat_invocation_is_noop(self):
        runner = _make_runner()
        runner._apply_skill_model_override(
            "k1", {"model": "gpt-5-mini", "provider": None, "base_url": None}, "fancy"
        )
        runner._pending_model_notes.pop("k1", None)
        _seed_cache(runner, "k1")
        runner._apply_skill_model_override(
            "k1", {"model": "gpt-5-mini", "provider": None, "base_url": None}, "fancy"
        )
        # Same override already active: no second eviction, no new note.
        assert "k1" in runner._agent_cache
        assert "k1" not in runner._pending_model_notes

    def test_empty_override_is_noop(self):
        runner = _make_runner()
        _seed_cache(runner, "k1")
        runner._apply_skill_model_override("k1", None, "fancy")
        runner._apply_skill_model_override(
            "k1", {"model": None, "provider": None, "base_url": None}, "fancy"
        )
        assert runner._session_model_overrides == {}
        assert "k1" in runner._agent_cache

    def test_resolution_failure_fails_open(self, monkeypatch):
        def boom(**kw):
            raise ValueError("no creds")

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider", boom
        )
        runner = _make_runner()
        _seed_cache(runner, "k1")
        runner._apply_skill_model_override(
            "k1", {"model": "m", "provider": "openai", "base_url": None}, "fancy"
        )
        assert runner._session_model_overrides == {}
        assert "k1" in runner._agent_cache

    def test_model_switch_overrides_previous_skill(self):
        runner = _make_runner()
        runner._apply_skill_model_override(
            "k1", {"model": "model-a", "provider": None, "base_url": None}, "skill-a"
        )
        runner._apply_skill_model_override(
            "k1", {"model": "model-b", "provider": None, "base_url": None}, "skill-b"
        )
        assert runner._session_model_overrides["k1"]["model"] == "model-b"


class TestApplySkillOverrideFromPath:
    def test_reads_frontmatter_and_applies(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            "---\n"
            "name: fancy\n"
            "description: x\n"
            "metadata:\n"
            "  hermes:\n"
            "    model: gpt-5-mini\n"
            "---\n\nbody\n",
            encoding="utf-8",
        )
        runner = _make_runner()
        runner._apply_skill_model_override_from_path("k1", str(skill_md), "fancy")
        assert runner._session_model_overrides["k1"]["model"] == "gpt-5-mini"

    def test_skill_without_override_is_noop(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            "---\nname: plain\ndescription: x\n---\n\nbody\n", encoding="utf-8"
        )
        runner = _make_runner()
        runner._apply_skill_model_override_from_path("k1", str(skill_md), "plain")
        assert runner._session_model_overrides == {}

    def test_missing_file_is_noop(self, tmp_path):
        runner = _make_runner()
        runner._apply_skill_model_override_from_path(
            "k1", str(tmp_path / "nope.md"), "ghost"
        )
        assert runner._session_model_overrides == {}
