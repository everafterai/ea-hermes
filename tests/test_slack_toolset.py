def test_slack_toolset_resolves_to_slack_react():
    import tools.slack_react_tool  # noqa: F401  (register the tool)
    from toolsets import resolve_toolset
    assert "slack_react" in resolve_toolset("slack")


def test_slack_platform_default_bundle_includes_slack_react():
    # The Slack platform loads the ``hermes-slack`` bundle by default. If
    # slack_react isn't in that bundle, the gateway's subset-inference resolver
    # never enables the ``slack`` toolset on a Slack turn, so the agent can't
    # react and falls back to typing the emoji as text. Mirror hermes-discord.
    import tools.slack_react_tool  # noqa: F401
    from toolsets import resolve_toolset
    assert "slack_react" in resolve_toolset("hermes-slack")


def test_get_platform_tools_enables_slack_toolset_for_slack_by_default():
    # End-to-end: with no explicit platform_toolsets config, the Slack platform's
    # resolved toolsets must include ``slack`` so slack_react reaches the agent.
    import tools.slack_react_tool  # noqa: F401
    from hermes_cli.tools_config import _get_platform_tools
    enabled = _get_platform_tools({}, "slack")
    assert "slack" in enabled
