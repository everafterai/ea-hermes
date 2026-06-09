"""Tests for the relevance pre-gate (quiet-channel classification bypass)."""


def test_message_event_directly_addressed_defaults_false():
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform
    ev = MessageEvent(text="hi", source=SessionSource(platform=Platform.SLACK, chat_id="C1"))
    assert ev.directly_addressed is False
