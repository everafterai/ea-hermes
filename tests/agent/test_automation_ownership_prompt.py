"""The ownership guidance is injected into the stable system-prompt segment."""
import types
from contextlib import ExitStack, contextmanager
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


# Helpers run_agent pulls in that make external calls or touch the filesystem.
# Stubbed so the system prompt builds deterministically. Upstream periodically
# adds, renames, or relocates these (``build_nous_subscription_prompt`` moved
# out of run_agent's namespace in v0.20.6), so patch only what is actually
# bound right now — a helper that no longer exists needs no stubbing, and
# hard-coding it turns an upstream refactor into a false failure here.
_STUBBED_RUN_AGENT_HELPERS = {
    "load_soul_md": "",
    "build_nous_subscription_prompt": "",
    "build_environment_hints": "",
    "build_context_files_prompt": "",
    "build_skills_system_prompt": "",
    "get_toolset_for_tool": None,
}


@contextmanager
def _patched_run_agent():
    """Patch run_agent helpers that make external calls or touch the filesystem."""
    import run_agent

    with ExitStack() as stack:
        for name, value in _STUBBED_RUN_AGENT_HELPERS.items():
            if hasattr(run_agent, name):
                stack.enter_context(patch(f"run_agent.{name}", return_value=value))
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
