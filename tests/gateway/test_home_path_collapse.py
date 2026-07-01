"""Home-dir collapse (OS home -> ~) at the gateway display boundaries."""

from gateway.config import Platform
from gateway.run import (
    _prepare_gateway_status_message,
    _sanitize_gateway_final_response,
)


def test_final_response_collapses_home_for_slack(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    out = _sanitize_gateway_final_response(
        Platform.SLACK, "I saved it to /home/testuser/.hermes/skills/foo"
    )
    assert out == "I saved it to ~/.hermes/skills/foo"


def test_status_message_collapses_home_for_slack(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    out = _prepare_gateway_status_message(
        Platform.SLACK, "lifecycle", "reading /home/testuser/.hermes/state.db"
    )
    assert out == "reading ~/.hermes/state.db"


def test_final_response_keeps_normal_answer(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    answer = "Here is the clean summary you asked for."
    assert _sanitize_gateway_final_response(Platform.SLACK, answer) == answer
