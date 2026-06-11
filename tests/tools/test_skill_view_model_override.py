"""skill_view exposes a skill's frontmatter model override as `model_override`."""

import json

import pytest


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return skills


def _write_skill(skills_dir, name, frontmatter_extra=""):
    skill_dir = skills_dir / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: test skill\n"
        f"{frontmatter_extra}"
        "---\n\n"
        "# Body\n",
        encoding="utf-8",
    )
    return skill_dir


class TestSkillViewModelOverride:
    def test_metadata_hermes_model_exposed(self, skills_dir):
        from tools.skills_tool import skill_view

        _write_skill(
            skills_dir,
            "fancy",
            "metadata:\n  hermes:\n    model: gpt-5-mini\n    provider: openai\n",
        )
        result = json.loads(skill_view("fancy", preprocess=False))
        assert result["success"] is True
        assert result["model_override"] == {
            "model": "gpt-5-mini",
            "provider": "openai",
            "base_url": None,
        }

    def test_no_override_is_none(self, skills_dir):
        from tools.skills_tool import skill_view

        _write_skill(skills_dir, "plain")
        result = json.loads(skill_view("plain", preprocess=False))
        assert result["success"] is True
        assert result["model_override"] is None
