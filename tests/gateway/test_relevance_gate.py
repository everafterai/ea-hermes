"""Tests for the relevance pre-gate (quiet-channel classification bypass)."""


def test_message_event_directly_addressed_defaults_false():
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform
    ev = MessageEvent(text="hi", source=SessionSource(platform=Platform.SLACK, chat_id="C1"))
    assert ev.directly_addressed is False


from gateway.config import Platform
from gateway.session import SessionSource
from gateway.run import _relevance_gate_purpose

_DEFAULT_PURPOSE = (
    "Decide whether the assistant must take an action relevant to this "
    "channel; otherwise ignore."
)


def _src(chat_id="C1", platform=Platform.SLACK):
    return SessionSource(platform=platform, chat_id=chat_id, chat_type="channel")


def test_purpose_none_when_not_quiet_channel():
    assert _relevance_gate_purpose(_src("C1"), {"slack": {"quiet_channels": "C2"}}) is None


def test_purpose_explicit_map_wins():
    cfg = {"slack": {"quiet_channels": "C1",
                     "relevance_gate_purpose": {"C1": "track bugs"},
                     "channel_prompts": {"C1": "behavior prompt"}}}
    assert _relevance_gate_purpose(_src("C1"), cfg) == "track bugs"


def test_purpose_falls_back_to_channel_prompt():
    cfg = {"slack": {"quiet_channels": "C1", "channel_prompts": {"C1": "behavior prompt"}}}
    assert _relevance_gate_purpose(_src("C1"), cfg) == "behavior prompt"


def test_purpose_falls_back_to_default():
    cfg = {"slack": {"quiet_channels": "C1"}}
    assert _relevance_gate_purpose(_src("C1"), cfg) == _DEFAULT_PURPOSE


import asyncio
import gateway.run as gr


class _FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


def _run(coro):
    return asyncio.run(coro)


# NOTE: _classify_relevance imports async_call_llm lazily from
# agent.auxiliary_client, so patch it on the SOURCE module (re-read each call).
def test_classify_act(monkeypatch):
    async def fake(**kw):
        return _FakeResp("act")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "", None)) is True


def test_classify_ignore(monkeypatch):
    async def fake(**kw):
        return _FakeResp("ignore")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "ctx", "gpt-5-nano")) is False


def test_classify_ignore_case_and_punctuation(monkeypatch):
    async def fake(**kw):
        return _FakeResp("  IGNORE.\n")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "", None)) is False


def test_classify_empty_or_garbage_fails_open_to_act(monkeypatch):
    async def fake(**kw):
        return _FakeResp("")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "", None)) is True


def test_classify_passes_model_through(monkeypatch):
    seen = {}
    async def fake(**kw):
        seen.update(kw)
        return _FakeResp("ignore")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    _run(gr._classify_relevance("PURPOSE", "the message", "the ctx", "gpt-5-nano"))
    assert seen.get("model") == "gpt-5-nano"
    assert seen.get("temperature") == 0
    blob = str(seen.get("messages"))
    assert "PURPOSE" in blob and "the message" in blob and "the ctx" in blob
