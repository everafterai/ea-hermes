"""Cron jobs pick up a skill-declared model override when the job has none.

Field-wise precedence: job.model/provider/base_url > skill frontmatter
override > config default (handled downstream in _run_job_impl).
"""

import json

import pytest

from cron.scheduler import (
    _effective_job_model_fields,
    _resolve_job_skills_model_override,
)


def _fake_skill_view(overrides_by_name):
    def _view(name, *args, **kwargs):
        entry = overrides_by_name.get(name)
        if entry is None:
            return json.dumps({"success": False, "error": "not found"})
        return json.dumps({"success": True, "name": name, "model_override": entry})

    return _view


class TestResolveJobSkillsModelOverride:
    def test_first_skill_with_override_wins(self, monkeypatch):
        monkeypatch.setattr(
            "tools.skills_tool.skill_view",
            _fake_skill_view(
                {
                    "plain": False and None,  # success but no override
                    "fancy": {"model": "gpt-5-mini", "provider": "openai", "base_url": None},
                    "other": {"model": "other-model", "provider": None, "base_url": None},
                }
            ),
        )
        # "plain" loads fine but has no override (None)
        job = {"id": "j1", "skills": ["plain", "fancy", "other"]}
        out = _resolve_job_skills_model_override(job)
        assert out["model"] == "gpt-5-mini"
        assert out["provider"] == "openai"

    def test_legacy_single_skill_field(self, monkeypatch):
        monkeypatch.setattr(
            "tools.skills_tool.skill_view",
            _fake_skill_view({"fancy": {"model": "m1", "provider": None, "base_url": None}}),
        )
        assert _resolve_job_skills_model_override({"skill": "fancy"})["model"] == "m1"

    def test_no_skills_is_none(self, monkeypatch):
        monkeypatch.setattr("tools.skills_tool.skill_view", _fake_skill_view({}))
        assert _resolve_job_skills_model_override({"id": "j1"}) is None

    def test_missing_skill_skipped(self, monkeypatch):
        monkeypatch.setattr(
            "tools.skills_tool.skill_view",
            _fake_skill_view({"fancy": {"model": "m1", "provider": None, "base_url": None}}),
        )
        job = {"skills": ["ghost", "fancy"]}
        assert _resolve_job_skills_model_override(job)["model"] == "m1"

    def test_skill_view_error_fails_open(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr("tools.skills_tool.skill_view", boom)
        assert _resolve_job_skills_model_override({"skills": ["fancy"]}) is None


class TestEffectiveJobModelFields:
    def test_job_fields_win_field_wise(self, monkeypatch):
        monkeypatch.setattr(
            "tools.skills_tool.skill_view",
            _fake_skill_view(
                {"fancy": {"model": "skill-model", "provider": "openai", "base_url": "https://s"}}
            ),
        )
        job = {"model": "job-model", "skills": ["fancy"]}
        model, provider, base_url = _effective_job_model_fields(job)
        assert model == "job-model"  # job wins
        assert provider == "openai"  # skill fills the gap
        assert base_url == "https://s"

    def test_skill_fills_all_when_job_has_none(self, monkeypatch):
        monkeypatch.setattr(
            "tools.skills_tool.skill_view",
            _fake_skill_view({"fancy": {"model": "skill-model", "provider": None, "base_url": None}}),
        )
        job = {"skills": ["fancy"]}
        model, provider, base_url = _effective_job_model_fields(job)
        assert model == "skill-model"
        assert provider is None
        assert base_url is None

    def test_no_skills_passthrough(self, monkeypatch):
        monkeypatch.setattr("tools.skills_tool.skill_view", _fake_skill_view({}))
        job = {"model": "job-model", "provider": "anthropic"}
        model, provider, base_url = _effective_job_model_fields(job)
        assert (model, provider, base_url) == ("job-model", "anthropic", None)
