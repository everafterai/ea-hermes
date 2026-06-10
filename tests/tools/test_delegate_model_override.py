"""Tests for per-call model/provider/base_url overrides on delegate_task.

Precedence (field-wise): tool-call params > delegation.* config (creds) >
inherit parent. Per-task fields beat top-level call fields.
"""

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task


def _make_mock_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _inherit_creds():
    return {
        "model": None,
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
    }


def _mock_child():
    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "api_calls": 1,
    }
    return child


class TestDelegateModelOverride(unittest.TestCase):
    def test_schema_exposes_model_params(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("model", props)
        self.assertIn("provider", props)
        self.assertIn("base_url", props)
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("model", task_props)
        self.assertIn("provider", task_props)
        self.assertIn("base_url", task_props)

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_model_param_reaches_child(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {"max_iterations": 45}
        mock_creds.return_value = _inherit_creds()
        parent = _make_mock_parent()

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child()
            delegate_task(goal="g", model="cheap-model", parent_agent=parent)
            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], "cheap-model")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_provider_param_resolves_bundle(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {"max_iterations": 45}
        mock_creds.return_value = _inherit_creds()
        parent = _make_mock_parent()

        with patch("run_agent.AIAgent") as MockAgent, patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "provider": "openai",
                "api_key": "sk-call",
                "base_url": "https://api.openai.com/v1",
                "api_mode": "chat_completions",
            },
        ):
            MockAgent.return_value = _mock_child()
            delegate_task(
                goal="g", model="gpt-5-mini", provider="openai", parent_agent=parent
            )
            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], "gpt-5-mini")
            self.assertEqual(kwargs["provider"], "openai")
            self.assertEqual(kwargs["api_key"], "sk-call")
            self.assertEqual(kwargs["base_url"], "https://api.openai.com/v1")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_call_model_beats_delegation_config_model(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {"max_iterations": 45, "model": "config-model"}
        mock_creds.return_value = {
            "model": "config-model",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
        parent = _make_mock_parent()

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child()
            delegate_task(goal="g", model="call-model", parent_agent=parent)
            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], "call-model")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_per_task_model_beats_top_level(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {"max_iterations": 45}
        mock_creds.return_value = _inherit_creds()
        parent = _make_mock_parent()

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child()
            delegate_task(
                tasks=[
                    {"goal": "a", "model": "task-model"},
                    {"goal": "b"},
                ],
                model="top-model",
                parent_agent=parent,
            )
            models = [c.kwargs["model"] for c in MockAgent.call_args_list]
            self.assertEqual(models, ["task-model", "top-model"])

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_omitted_params_inherit(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {"max_iterations": 45}
        mock_creds.return_value = _inherit_creds()
        parent = _make_mock_parent()

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child()
            delegate_task(goal="g", parent_agent=parent)
            _, kwargs = MockAgent.call_args
            # No overrides anywhere → child inherits the parent's model.
            self.assertEqual(kwargs["model"], parent.model)

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_resolution_failure_is_tool_error(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {"max_iterations": 45}
        mock_creds.return_value = _inherit_creds()
        parent = _make_mock_parent()

        def boom(**kw):
            raise ValueError("no credentials for 'openai'")

        with patch("run_agent.AIAgent") as MockAgent, patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=boom
        ):
            MockAgent.return_value = _mock_child()
            result = delegate_task(
                goal="g", model="m", provider="openai", parent_agent=parent
            )
            parsed = json.loads(result)
            self.assertIn("error", parsed)
            self.assertIn("openai", parsed["error"])
            MockAgent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
