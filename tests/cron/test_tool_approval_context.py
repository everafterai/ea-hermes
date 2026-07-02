from cron.tool_approval_context import (
    set_cron_tool_context, clear_cron_tool_context,
    get_cron_tool_context, in_cron_run,
)


def test_cron_tool_context_roundtrip():
    assert in_cron_run() is False
    tok = set_cron_tool_context(owner_grant=frozenset({"mcp-stripe"}),
                                acked_tools=["stripe_api_write"])
    try:
        assert in_cron_run() is True
        grant, acked = get_cron_tool_context()
        assert grant == frozenset({"mcp-stripe"})
        assert acked == frozenset({"stripe_api_write"})
    finally:
        clear_cron_tool_context(tok)
    assert in_cron_run() is False
