"""Tests for per-channel model overrides (slack.channel_models).

Covers the resolver (gateway/platforms/base.py:resolve_channel_model) and
its application inside GatewayRunner._resolve_session_agent_runtime —
precedence: session /model override > channel_models > global default.
"""

import threading

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import resolve_channel_model
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# resolve_channel_model
# ---------------------------------------------------------------------------


class TestResolveChannelModel:
    def test_exact_match_string(self):
        extra = {"channel_models": {"C1": "gpt-5-mini"}}
        assert resolve_channel_model(extra, "C1") == "gpt-5-mini"

    def test_parent_fallback(self):
        extra = {"channel_models": {"C1": "gpt-5-mini"}}
        assert resolve_channel_model(extra, "C1.thread", "C1") == "gpt-5-mini"

    def test_exact_beats_parent(self):
        extra = {"channel_models": {"C1": "parent-model", "T1": "thread-model"}}
        assert resolve_channel_model(extra, "T1", "C1") == "thread-model"

    def test_no_match_is_none(self):
        extra = {"channel_models": {"C1": "gpt-5-mini"}}
        assert resolve_channel_model(extra, "C2") is None
        assert resolve_channel_model({}, "C1") is None

    def test_blank_entry_is_absent(self):
        extra = {"channel_models": {"C1": "   "}}
        assert resolve_channel_model(extra, "C1") is None

    def test_dict_entry_returned_raw(self):
        entry = {"model": "m", "provider": "openai"}
        extra = {"channel_models": {"C1": entry}}
        assert resolve_channel_model(extra, "C1") == entry

    def test_non_dict_config_is_none(self):
        assert resolve_channel_model({"channel_models": "oops"}, "C1") is None


# ---------------------------------------------------------------------------
# _resolve_session_agent_runtime integration
# ---------------------------------------------------------------------------


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._session_model_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    return runner


def _slack_source(chat_id="C1", parent=None):
    return SessionSource(
        platform=Platform.SLACK,
        chat_id=chat_id,
        chat_type="channel",
        user_id="U1",
        parent_chat_id=parent,
    )


def _cfg(channel_models):
    return {
        "model": {"default": "global-model"},
        "slack": {"channel_models": channel_models},
    }


@pytest.fixture
def quiet_runtime(monkeypatch):
    """Neutralize env-based runtime resolution."""
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {})


class TestChannelModelRuntime:
    def test_string_entry_overrides_global(self, quiet_runtime):
        runner = _make_runner()
        model, _ = runner._resolve_session_agent_runtime(
            source=_slack_source(),
            session_key="k1",
            user_config=_cfg({"C1": "channel-model"}),
        )
        assert model == "channel-model"

    def test_no_entry_keeps_global(self, quiet_runtime):
        runner = _make_runner()
        model, _ = runner._resolve_session_agent_runtime(
            source=_slack_source("C9"),
            session_key="k1",
            user_config=_cfg({"C1": "channel-model"}),
        )
        assert model == "global-model"

    def test_thread_inherits_parent_channel_model(self, quiet_runtime):
        runner = _make_runner()
        model, _ = runner._resolve_session_agent_runtime(
            source=_slack_source("C1.99", parent="C1"),
            session_key="k1",
            user_config=_cfg({"C1": "channel-model"}),
        )
        assert model == "channel-model"

    def test_dict_entry_resolves_provider_bundle(self, quiet_runtime, monkeypatch):
        captured = {}

        def fake_resolve(**kwargs):
            captured.update(kwargs)
            return {
                "provider": "openai",
                "api_key": "sk-chan",
                "base_url": "https://api.openai.com/v1",
                "api_mode": "chat_completions",
            }

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve
        )
        runner = _make_runner()
        model, runtime = runner._resolve_session_agent_runtime(
            source=_slack_source(),
            session_key="k1",
            user_config=_cfg({"C1": {"model": "gpt-5-mini", "provider": "openai"}}),
        )
        assert model == "gpt-5-mini"
        assert captured["requested"] == "openai"
        assert runtime["provider"] == "openai"
        assert runtime["api_key"] == "sk-chan"

    def test_session_override_fast_path_beats_channel(self, quiet_runtime):
        runner = _make_runner()
        runner._session_model_overrides["k1"] = {
            "model": "user-model",
            "provider": "openai",
            "api_key": "sk-user",
            "base_url": "https://api.openai.com/v1",
            "api_mode": "chat_completions",
        }
        model, runtime = runner._resolve_session_agent_runtime(
            source=_slack_source(),
            session_key="k1",
            user_config=_cfg({"C1": "channel-model"}),
        )
        assert model == "user-model"
        assert runtime["api_key"] == "sk-user"

    def test_session_override_without_api_key_beats_channel(self, quiet_runtime):
        runner = _make_runner()
        runner._session_model_overrides["k1"] = {"model": "user-model"}
        model, _ = runner._resolve_session_agent_runtime(
            source=_slack_source(),
            session_key="k1",
            user_config=_cfg({"C1": "channel-model"}),
        )
        assert model == "user-model"

    def test_non_slack_source_ignores_channel_models(self, quiet_runtime):
        runner = _make_runner()
        source = SessionSource(
            platform=Platform.LOCAL, chat_id="C1", chat_type="dm", user_id="U1"
        )
        model, _ = runner._resolve_session_agent_runtime(
            source=source,
            session_key="k1",
            user_config=_cfg({"C1": "channel-model"}),
        )
        assert model == "global-model"

    def test_credential_failure_falls_back_to_global(self, quiet_runtime, monkeypatch):
        def fake_resolve(**kwargs):
            raise ValueError("no credentials")

        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve
        )
        runner = _make_runner()
        model, runtime = runner._resolve_session_agent_runtime(
            source=_slack_source(),
            session_key="k1",
            user_config=_cfg({"C1": {"model": "m", "provider": "openai"}}),
        )
        assert model == "global-model"
        assert runtime.get("api_key") is None

    def test_adapter_extra_preferred_over_raw_config(self, quiet_runtime):
        class _Adapter:
            class config:
                extra = {"channel_models": {"C1": "adapter-model"}}

        runner = _make_runner()
        runner.adapters = {Platform.SLACK: _Adapter()}
        model, _ = runner._resolve_session_agent_runtime(
            source=_slack_source(),
            session_key="k1",
            user_config=_cfg({"C1": "raw-model"}),
        )
        assert model == "adapter-model"

    def test_no_source_no_crash(self, quiet_runtime):
        runner = _make_runner()
        model, _ = runner._resolve_session_agent_runtime(
            source=None,
            session_key="k1",
            user_config=_cfg({"C1": "channel-model"}),
        )
        assert model == "global-model"
