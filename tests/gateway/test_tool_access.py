"""Unit tests for gateway.tool_access — per-user tool RBAC.

Tests the pure policy resolver (no gateway plumbing). Integration tests that
exercise the enforcement sites live in test_tool_access_enforcement.py.
"""
from __future__ import annotations

from gateway.tool_access import (
    BUILTIN_ROLES,
    ToolAccessPolicy,
    policy_from_extra,
    policy_for_source,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from gateway.session import SessionSource


ALL_TOOLSETS = frozenset(
    {"terminal", "file", "web", "browser", "vision", "memory",
     "delegation", "session_search", "mcp-github", "mcp-jira"}
)


class TestPolicyFromExtra:
    def test_empty_extra_is_disabled(self):
        p = policy_from_extra({})
        assert p.enabled is False

    def test_disabled_policy_authorizes_anyone(self):
        # When RBAC is off, callers fall back to legacy auth; the policy
        # must not deny. is_authorized short-circuits True so the gate defers.
        p = policy_from_extra({})
        assert p.is_authorized("U_ANYONE") is True
        assert p.allowed_toolsets("U_ANYONE", ALL_TOOLSETS) == ALL_TOOLSETS

    def test_user_roles_activates_policy(self):
        p = policy_from_extra({"user_roles": {"U_A": "admin"}})
        assert p.enabled is True

    def test_builtin_roles_available_without_roles_block(self):
        p = policy_from_extra({"user_roles": {"U_A": "readonly"}})
        assert p.role_for("U_A") == "readonly"
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset(BUILTIN_ROLES["readonly"]) & ALL_TOOLSETS

    def test_id_and_role_coercion(self):
        # YAML may load int IDs and pad whitespace.
        p = policy_from_extra({"user_roles": {123: " admin ", "U_B ": "Operator"}})
        assert p.role_for("123") == "admin"
        assert p.role_for("U_B") == "operator"


class TestToolsetResolution:
    def test_admin_wildcard_grants_everything(self):
        p = policy_from_extra({"user_roles": {"U_A": "admin"}})
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == ALL_TOOLSETS
        assert p.can_use_tool("U_A", "terminal") is True

    def test_chat_only_grants_nothing(self):
        p = policy_from_extra({"user_roles": {"U_A": "chat_only"}})
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset()
        assert p.can_use_tool("U_A", "terminal") is False
        assert p.is_authorized("U_A") is True  # may still chat

    def test_explicit_toolset_list(self):
        p = policy_from_extra(
            {"roles": {"limited": {"toolsets": ["web", "vision"]}},
             "user_roles": {"U_A": "limited"}}
        )
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset({"web", "vision"})
        assert p.can_use_tool("U_A", "web") is True
        assert p.can_use_tool("U_A", "terminal") is False

    def test_mcp_glob(self):
        p = policy_from_extra(
            {"roles": {"mcpuser": {"toolsets": ["mcp-*"]}},
             "user_roles": {"U_A": "mcpuser"}}
        )
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset({"mcp-github", "mcp-jira"})
        assert p.can_use_tool("U_A", "mcp-github") is True
        assert p.can_use_tool("U_A", "terminal") is False

    def test_custom_role_overrides_builtin(self):
        p = policy_from_extra(
            {"roles": {"readonly": {"toolsets": ["web"]}},
             "user_roles": {"U_A": "readonly"}}
        )
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset({"web"})

    def test_scalar_string_toolsets(self):
        # A bare YAML string like `toolsets: web, vision` should be treated as
        # comma-separated rather than silently dropped.
        p = policy_from_extra(
            {"roles": {"r": {"toolsets": "web, vision"}},
             "user_roles": {"U_A": "r"}}
        )
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset({"web", "vision"})

    def test_toolset_match_is_case_insensitive(self):
        # A caller passing "WEB" should match a role granting ["web"].
        p = policy_from_extra(
            {"roles": {"r": {"toolsets": ["web"]}},
             "user_roles": {"U_A": "r"}}
        )
        assert p.can_use_tool("U_A", "WEB") is True


class TestFailClosed:
    def test_unassigned_user_denied(self):
        p = policy_from_extra({"user_roles": {"U_A": "admin"}})
        assert p.is_authorized("U_STRANGER") is False
        assert p.allowed_toolsets("U_STRANGER", ALL_TOOLSETS) == frozenset()
        assert p.can_use_tool("U_STRANGER", "web") is False

    def test_undefined_role_denied(self):
        p = policy_from_extra({"user_roles": {"U_A": "ghost"}})
        assert p.is_authorized("U_A") is False
        assert p.allowed_toolsets("U_A", ALL_TOOLSETS) == frozenset()


class TestPolicyForSource:
    def test_resolves_slack_extra(self):
        cfg = GatewayConfig()
        cfg.platforms[Platform.SLACK] = PlatformConfig(
            extra={"user_roles": {"U_A": "operator"}}
        )
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_A")
        p = policy_for_source(cfg, src)
        assert p.enabled is True
        assert p.role_for("U_A") == "operator"

    def test_missing_platform_is_disabled(self):
        cfg = GatewayConfig()
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", user_id="U_A")
        assert policy_for_source(cfg, src).enabled is False


class TestConfigBridge:
    def test_roles_and_user_roles_reach_slack_extra(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "slack:\n"
            "  enabled: true\n"
            "  roles:\n"
            "    limited:\n"
            "      toolsets:\n"
            "        - web\n"
            "  user_roles:\n"
            "    U_A: limited\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.platforms[Platform.SLACK].extra["roles"] == {
            "limited": {"toolsets": ["web"]}
        }
        assert config.platforms[Platform.SLACK].extra["user_roles"] == {"U_A": "limited"}


class TestFloorToolsets:
    def test_chat_only_gets_floor_but_no_other_tools(self):
        p = policy_from_extra({"user_roles": {"U_A": "chat_only"}})
        allowed = p.allowed_toolsets("U_A", ALL_TOOLSETS | {"clarify", "todo"})
        assert "clarify" in allowed
        assert "todo" in allowed
        assert "terminal" not in allowed
        assert p.can_use_tool("U_A", "clarify") is True
        assert p.can_use_tool("U_A", "todo") is True
        assert p.can_use_tool("U_A", "terminal") is False
        # chat_only is still authorized to interact
        assert p.is_authorized("U_A") is True

    def test_floor_added_to_restricted_role(self):
        p = policy_from_extra(
            {"roles": {"limited": {"toolsets": ["web"]}},
             "user_roles": {"U_A": "limited"}}
        )
        assert p.can_use_tool("U_A", "web") is True
        assert p.can_use_tool("U_A", "clarify") is True  # floor
        assert p.can_use_tool("U_A", "terminal") is False

    def test_floor_does_not_rescue_roleless_user(self):
        p = policy_from_extra({"user_roles": {"U_A": "chat_only"}})
        # A user with no role assignment gets nothing — not even the floor.
        assert p.can_use_tool("U_STRANGER", "clarify") is False
        assert p.allowed_toolsets("U_STRANGER", {"clarify", "todo", "web"}) == frozenset()
        assert p.is_authorized("U_STRANGER") is False

    def test_floor_does_not_rescue_undefined_role(self):
        p = policy_from_extra({"user_roles": {"U_A": "ghost"}})
        assert p.can_use_tool("U_A", "clarify") is False
        assert p.allowed_toolsets("U_A", {"clarify", "todo"}) == frozenset()

    def test_floor_noop_when_disabled(self):
        # When RBAC is off, everything is allowed anyway; floor changes nothing.
        p = policy_from_extra({})
        assert p.can_use_tool("U_A", "clarify") is True
        assert p.can_use_tool("U_A", "terminal") is True


class TestSlackReactToolsetGating:
    def test_slack_react_maps_to_slack_toolset(self):
        import tools.slack_react_tool  # noqa: F401  (register the tool)
        from tools.registry import registry
        assert registry.get_toolset_for_tool("slack_react") == "slack"

    def test_slack_toolset_is_floor_for_every_valid_role(self):
        # slack_react + turn_end are UX/turn-control tools, not privileges:
        # every valid-role user gets them regardless of their role's grant,
        # so the bot can acknowledge/close out ANY user's Slack turn.
        p = policy_from_extra({
            "user_roles": {
                "U_react": "reactor",
                "U_chat": "chat_only",
                "U_ro": "readonly",
            },
            "roles": {"reactor": ["slack"], "chat_only": []},
        })
        assert p.can_use_tool("U_react", "slack") is True
        assert p.can_use_tool("U_chat", "slack") is True   # floor, despite []
        assert p.can_use_tool("U_ro", "slack") is True     # floor, readonly grant lacks it
        # And it survives the toolset filter for a restricted role.
        assert "slack" in p.allowed_toolsets("U_chat", frozenset({"slack", "terminal"}))
        assert "terminal" not in p.allowed_toolsets("U_chat", frozenset({"slack", "terminal"}))

    def test_slack_floor_does_not_rescue_roleless_or_undefined(self):
        # Deny-until-assigned still wins: no role / undefined role gets nothing,
        # not even the slack floor.
        p = policy_from_extra({"user_roles": {"U_A": "chat_only", "U_ghost": "ghost"}})
        assert p.can_use_tool("U_STRANGER", "slack") is False
        assert p.can_use_tool("U_ghost", "slack") is False
        assert p.allowed_toolsets("U_STRANGER", frozenset({"slack"})) == frozenset()

    def test_admin_wildcard_allows_slack_toolset(self):
        p = policy_from_extra({
            "user_roles": {"U_admin": "admin"},
            "roles": {"admin": ["*"]},
        })
        assert p.can_use_tool("U_admin", "slack") is True


class TestChannelRoles:
    """A ``channel_roles`` map ({chat_id: role}) lets the bot serve EVERY user
    in a configured channel under a fixed service role — used for issue-tracking
    channels where any teammate may report and the bot must still run `ntn`
    (terminal). The channel grant is UNIONed with the poster's own role; it
    only ADDS access, in the listed channels, and only while RBAC is active."""

    def _policy(self):
        return policy_from_extra({
            "user_roles": {"U_admin": "admin", "U_ro": "readonly"},
            "roles": {"tracker": {"toolsets": ["terminal"]}},
            "channel_roles": {"C_track": "tracker"},
        })

    def test_channel_role_authorizes_any_user_in_that_channel(self):
        p = self._policy()
        # Roleless user, but posting in the tracked channel → authorized.
        assert p.is_authorized("U_stranger", "C_track") is True
        # Same user elsewhere → still denied (deny-until-assigned holds).
        assert p.is_authorized("U_stranger", "C_other") is False
        # And with no channel context at all (e.g. CLI) → denied.
        assert p.is_authorized("U_stranger") is False

    def test_channel_role_grants_its_toolsets_to_any_poster(self):
        p = self._policy()
        assert p.can_use_tool("U_stranger", "terminal", "C_track") is True
        assert p.can_use_tool("U_stranger", "terminal", "C_other") is False
        assert p.can_use_tool("U_stranger", "terminal") is False
        allowed = p.allowed_toolsets("U_stranger", frozenset({"terminal", "web"}), "C_track")
        assert "terminal" in allowed
        assert "web" not in allowed  # tracker grants only terminal

    def test_channel_role_unions_with_user_role(self):
        p = self._policy()
        # readonly user in the tracked channel: keeps readonly's web AND gains
        # the channel's terminal.
        assert p.can_use_tool("U_ro", "web", "C_track") is True
        assert p.can_use_tool("U_ro", "terminal", "C_track") is True
        # Outside the tracked channel: only their own readonly grant.
        assert p.can_use_tool("U_ro", "terminal", "C_other") is False
        assert p.can_use_tool("U_ro", "web", "C_other") is True

    def test_channel_role_grants_floor_to_roleless_poster(self):
        p = self._policy()
        # The bot can still react / clarify for a roleless poster in-channel.
        assert p.can_use_tool("U_stranger", "slack", "C_track") is True
        assert p.can_use_tool("U_stranger", "clarify", "C_track") is True

    def test_undefined_channel_role_denies_that_axis(self):
        p = policy_from_extra({
            "user_roles": {"U_admin": "admin"},
            "channel_roles": {"C_track": "ghost"},  # undefined role
        })
        assert p.is_authorized("U_stranger", "C_track") is False
        # A real admin posting there is still authorized via their own role.
        assert p.is_authorized("U_admin", "C_track") is True

    def test_channel_roles_inert_when_rbac_disabled(self):
        # No user_roles → RBAC stays OFF (activation boundary unchanged);
        # channel_roles must not silently switch RBAC on.
        p = policy_from_extra({"channel_roles": {"C_track": "operator"}})
        assert p.enabled is False
        assert p.is_authorized("U_stranger", "C_track") is True  # defers to legacy
        assert p.allowed_toolsets("U_stranger", ALL_TOOLSETS, "C_track") == ALL_TOOLSETS


class TestNotionGrant:
    def test_operator_grants_notion(self):
        p = policy_from_extra({"user_roles": {"U_OP": "operator"}})
        assert p.can_use_tool("U_OP", "notion") is True
        assert p.can_use_tool("U_OP", "session_search") is True
        assert p.can_use_tool("U_OP", "terminal") is False  # operator has no shell

    def test_channel_operator_grants_notion_to_any_poster(self):
        # An issue channel maps every poster to operator; a roleless poster must
        # get notion there — but NOT terminal (operator has no shell).
        p = policy_from_extra({
            "user_roles": {"U_ADMIN": "admin"},          # activates RBAC
            "channel_roles": {"C_ISSUES": "operator"},
        })
        toolsets = frozenset({"notion", "terminal", "web"})
        got = p.allowed_toolsets("U_RANDO", toolsets, chat_id="C_ISSUES")
        assert "notion" in got
        assert "terminal" not in got


class TestChannelRolesConfigBridge:
    def test_channel_roles_reach_slack_extra(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "slack:\n"
            "  enabled: true\n"
            "  channel_roles:\n"
            "    C03B4BC9D2P: operator\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        config = load_gateway_config()
        assert config.platforms[Platform.SLACK].extra["channel_roles"] == {
            "C03B4BC9D2P": "operator"
        }
