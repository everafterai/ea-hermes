from hermes_cli.tools_config import _toolset_allowed_for_platform


def test_slack_toolset_only_on_slack():
    assert _toolset_allowed_for_platform("slack", "slack") is True
    assert _toolset_allowed_for_platform("slack", "discord") is False
    assert _toolset_allowed_for_platform("slack", "telegram") is False
