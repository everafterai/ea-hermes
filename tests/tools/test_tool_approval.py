import tools.approval as ap


def test_tool_requires_approval_matches_exact_and_glob(monkeypatch):
    monkeypatch.setattr(
        ap, "_get_approval_config",
        lambda: {"require_for_tools": ["stripe_api_write", "send_*"]},
    )
    assert ap.tool_requires_approval("stripe_api_write") is True
    assert ap.tool_requires_approval("send_message") is True
    assert ap.tool_requires_approval("SEND_MESSAGE") is True      # case-insensitive
    assert ap.tool_requires_approval("read_file") is False


def test_tool_requires_approval_inert_when_unset(monkeypatch):
    monkeypatch.setattr(ap, "_get_approval_config", lambda: {})
    assert ap.tool_requires_approval("stripe_api_write") is False
