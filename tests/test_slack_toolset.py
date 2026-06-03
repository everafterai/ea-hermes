def test_slack_toolset_resolves_to_slack_react():
    import tools.slack_react_tool  # noqa: F401  (register the tool)
    from toolsets import resolve_toolset
    assert "slack_react" in resolve_toolset("slack")
