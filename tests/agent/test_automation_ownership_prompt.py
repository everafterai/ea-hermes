"""The ownership guidance is injected into the stable system-prompt segment."""
import types
from contextlib import contextmanager
from unittest.mock import patch

import agent.automation_ownership as ao
import agent.system_prompt as sp


def _fake_agent(tool_names, model="claude-opus-4-8"):
    return types.SimpleNamespace(
        valid_tool_names=set(tool_names),
        model=model,
        load_soul_identity=False,
        skip_context_files=True,
        _task_completion_guidance=False,
        _tool_use_enforcement="never",
        # Additional attrs required by build_system_prompt_parts
        provider="",
        platform="",
        _kanban_worker_guidance="",
        _environment_probe=False,
        _memory_store=None,
        _memory_manager=None,
        pass_session_id=False,
        session_id="",
    )


@contextmanager
def _patched_run_agent():
    """Patch run_agent helpers that make external calls or touch the filesystem."""
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
        patch("run_agent.build_skills_system_prompt", return_value=""),
        patch("run_agent.get_toolset_for_tool", return_value=None),
    ):
        yield


def test_guidance_present_when_enabled_and_tool_available(monkeypatch):
    monkeypatch.setattr(ao, "is_enabled", lambda: True)
    with _patched_run_agent():
        parts = sp.build_system_prompt_parts(_fake_agent({"skill_manage"}))
    assert "owned by" in parts["stable"].lower() or "ownership" in parts["stable"].lower()


def test_guidance_absent_when_disabled(monkeypatch):
    monkeypatch.setattr(ao, "is_enabled", lambda: False)
    with _patched_run_agent():
        parts = sp.build_system_prompt_parts(_fake_agent({"skill_manage"}))
    assert "automation ownership" not in parts["stable"].lower()


def test_guidance_absent_without_editing_tool(monkeypatch):
    monkeypatch.setattr(ao, "is_enabled", lambda: True)
    with _patched_run_agent():
        parts = sp.build_system_prompt_parts(_fake_agent({"web_search"}))
    assert "automation ownership" not in parts["stable"].lower()
