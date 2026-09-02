"""Fork feature inventory — every EverAfter-specific capability must stay wired.

This is a merge guard, not a unit test. The 2026-08-24 upstream sync showed the
real risk of a big merge is not conflicts (those are visible) but fork code that
merges cleanly and stops running because upstream moved the call site out from
under it. Two live examples that shipped clean and were caught only by running
things: ``backfill_session_scope`` lost the line that executed its writer, and
``hermes_cli/main.py`` kept an inline ``tools`` subparser after upstream
extracted it — leaving a ``NameError: tools_sub`` that broke every CLI parse.

Each entry asserts a *call site*, not just that a symbol exists somewhere. When
one fails after a merge, the feature is silently disabled — find where upstream
moved the anchor and re-apply, do not delete the assertion.

See docs/superpowers/plans/2026-08-24-upstream-sync.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# (feature, file, needle, what the needle proves)
WIRING = [
    # ── RBAC: the three enforcement points ────────────────────────────────
    ("RBAC policy", "gateway/tool_access.py", "FLOOR_TOOLSETS", "floor toolsets defined"),
    ("RBAC policy", "gateway/tool_access.py", "BUILTIN_ROLES", "built-in roles defined"),
    ("RBAC policy", "gateway/tool_access.py", "def _effective_grant", "channel_roles union"),
    ("RBAC point A", "gateway/authz_mixin.py", "policy_for_source", "message gate in authz mixin"),
    ("RBAC point A", "gateway/authz_mixin.py", "_rbac_policy.is_authorized", "authorization decision wired"),
    ("RBAC point B", "gateway/run.py", "return filter_enabled_toolsets(", "toolset filter in resolver"),
    ("RBAC point C", "model_tools.py", "denial_for_current_tool", "execution backstop"),
    # ── RBAC admin surfaces ───────────────────────────────────────────────
    ("hermes users", "hermes_cli/users.py", "def register_users_subcommands", "registrar"),
    ("hermes users", "hermes_cli/main.py", "register_users_subcommands(subparsers)", "registered in main"),
    ("hermes tools rbac", "hermes_cli/tools_list.py", "def register_tools_rbac_subcommand", "registrar"),
    ("hermes tools rbac", "hermes_cli/subcommands/tools.py", "register_tools_rbac_subcommand(tools_sub)", "hooked into parser"),
    # ── Fork-only toolsets ────────────────────────────────────────────────
    ("notion_api", "tools/notion_api_tool.py", 'toolset="notion"', "own toolset"),
    ("jira_api", "tools/jira_api_tool.py", "_ALLOWED_METHODS", "read-only guard"),
    ("slack_post_thread", "tools/slack_post_thread_tool.py", 'toolset="slack_post"', "non-floor toolset"),
    ("webflow_asset_upload", "tools/webflow_asset_tool.py", "is_protected_data_path", "credential-read guard"),
    ("ownership tool", "tools/ownership_tool.py", 'toolset="ownership"', "floor toolset"),
    # ── Multi-user session isolation ──────────────────────────────────────
    ("Session visibility", "hermes_state.py", "def build_visibility_where", "SQL scope fragment"),
    ("Session visibility", "hermes_state.py", "def session_row_visible", "row-level gate"),
    ("Session scope backfill", "hermes_state.py", "self._execute_write(_do)", "backfill actually writes"),
    ("search_messages gate", "hermes_state_search.py", "build_visibility_where(scope", "SQL gate applied"),
    ("session_search gate", "tools/session_search_tool.py", "resolve_search_scope()", "scope resolved once"),
    ("session_search title gate", "tools/session_search_tool.py", "if not session_row_visible(session_meta, scope)", "title path gated"),
    ("reconcile via async boundary", "gateway/run.py", "await self.async_session_store.reconcile_db_scope()", "does not block the loop"),
    # ── Cross-user data protection ────────────────────────────────────────
    ("Protected paths", "agent/file_safety.py", "def is_protected_data_path", "matcher"),
    ("Protected paths", "tools/file_tools.py", "is_protected_data_path", "enforced in file tools"),
    ("Data-access audit", "agent/data_access_audit.py", "def record_access", "audit writer"),
    ("Data-access audit", "tools/terminal_tool.py", "record_command_access", "terminal logs access"),
    # ── Automation ownership ──────────────────────────────────────────────
    ("Ownership gate", "agent/automation_ownership.py", "def check_edit", "soft edit gate"),
    ("Ownership gate", "agent/automation_ownership.py", "def _require_owner_or_admin", "shared owner check"),
    ("Ownership: skills", "tools/skill_manager_tool.py", "_CONFIRM_OWNER", "confirm token wired"),
    ("Ownership: cron", "tools/cronjob_tools.py", "register_creator", "creator registered"),
    ("Ownership: files", "tools/file_tools.py", "_automation_ownership_check", "write/patch gated"),
    ("hermes own", "hermes_cli/own.py", "def run_own", "CLI entrypoint"),
    ("Ownership guidance", "agent/system_prompt.py", "AUTOMATION_OWNERSHIP_GUIDANCE", "in stable segment"),
    # ── Cron RBAC ceiling ─────────────────────────────────────────────────
    ("Cron ceiling", "cron/rbac_ceiling.py", "def apply_cron_toolset_ceiling", "ceiling fn"),
    ("Cron ceiling", "cron/scheduler.py", "enabled_toolsets=_cron_enabled_toolsets_with_ceiling(job, _cfg)", "wired at AIAgent build"),
    ("Cron create gate", "tools/cronjob_tools.py", "_rbac_creation_error", "create/update gate"),
    ("Cron unattended ack", "tools/cronjob_tools.py", "_unattended_ack_error", "ack gate"),
    ("Cron approval context", "cron/tool_approval_context.py", "def set_cron_tool_context", "owner grant export"),
    # ── Quiet channels / silent completion ────────────────────────────────
    ("Quiet channels", "gateway/run.py", "def _is_quiet_channel", "resolver"),
    ("Quiet channels", "plugins/platforms/slack/adapter.py", "def _slack_quiet_channels", "adapter half"),
    ("Quiet channels", "plugins/platforms/slack/adapter.py", "in self._slack_quiet_channels()", "reactions suppressed"),
    ("Silent completion", "gateway/run.py", "agent._silent_completion_ok = _is_quiet_channel", "set per turn"),
    ("turn_end", "agent/conversation_loop.py", "_called_terminal_turn_end", "loop honours turn_end"),
    ("Silent empty net", "agent/conversation_loop.py", "_should_accept_silent_empty", "empty-turn safety net"),
    ("Codex silent finish", "agent/conversation_loop.py", "_codex_incomplete_exhausted_result", "quiet codex path"),
    ("slack_react", "tools/slack_react_tool.py", "HERMES_SESSION_MESSAGE_ID", "targets triggering message"),
    ("slack_react targeting", "plugins/platforms/slack/adapter.py", "message_id=ts,", "source carries the ts"),
    # ── Relevance pre-gate ────────────────────────────────────────────────
    ("Relevance gate", "gateway/run.py", "_relevance_gate_should_skip", "invoked in dispatch"),
    ("Relevance bypass", "gateway/platforms/base.py", "directly_addressed", "MessageEvent field"),
    ("Relevance bypass", "plugins/platforms/slack/adapter.py", "directly_addressed=bool(is_one_to_one_dm or is_mentioned)", "adapter sets it"),
    # ── Model overrides ───────────────────────────────────────────────────
    ("Channel models", "gateway/run.py", "def _apply_channel_model_override", "per-channel"),
    ("Skill models", "gateway/run.py", "_apply_skill_model_override", "per-skill"),
    ("Cron skill models", "cron/scheduler.py", "def _effective_job_model_fields", "job/skill precedence"),
    ("Delegation models", "tools/delegate_tool.py", "_override_runtime_cache", "per-task override"),
    # ── Per-tool approval gate ────────────────────────────────────────────
    ("Approval gate", "tools/approval.py", "def tool_requires_approval", "require_for_tools glob"),
    ("Approval gate", "tools/approval.py", "def check_tool_approval", "entrypoint"),
    # ── Display / memory ──────────────────────────────────────────────────
    ("Home-path collapse", "agent/redact.py", "def collapse_home_path", "helper"),
    ("Home-path collapse", "agent/display.py", "return collapse_home_path(preview) if preview else preview", "applied at boundary"),
    ("Slack author anchor", "plugins/platforms/slack/adapter.py", "def _anchor_message_author", "helper"),
    ("Slack author anchor", "plugins/platforms/slack/adapter.py", "text = _anchor_message_author(text, user_name, is_dm)", "call site"),
    ("Memory global-only", "tools/memory_tool.py", "def memory_schema_for", "global-only schema"),
    ("Holographic scopes", "plugins/memory/holographic/__init__.py", "def _bundle_for_current_scope", "per-scope stores"),
    # ── Config plumbing ───────────────────────────────────────────────────
    ("Config bridge", "gateway/config.py", 'bridged["user_roles"]', "user_roles -> extra"),
    ("Config bridge", "gateway/config.py", 'bridged["user_names"]', "user_names -> extra"),
    ("Config bridge", "gateway/config.py", 'bridged["quiet_channels"]', "quiet_channels -> extra"),
    ("Config defaults", "hermes_cli/config_defaults.py", '"quiet_channels"', "fork slack defaults"),
    ("Slack yaml hook", "plugins/platforms/slack/adapter.py", "SLACK_HOME_CHANNEL_PROMPT", "home_channel_prompt bridged"),
]


@pytest.mark.parametrize(
    "feature,relpath,needle,proves",
    WIRING,
    ids=[f"{f}:{Path(p).name}:{i}" for i, (f, p, _, _) in enumerate(WIRING)],
)
def test_fork_feature_is_wired(feature, relpath, needle, proves):
    path = ROOT / relpath
    assert path.exists(), (
        f"{feature}: {relpath} is missing. Upstream may have moved or deleted it — "
        f"find the new home and re-apply the fork change."
    )
    text = path.read_text(encoding="utf-8", errors="replace")
    assert needle in text, (
        f"{feature}: {relpath} no longer contains the call site that proves "
        f"'{proves}'.\n"
        f"  expected: {needle}\n"
        f"This usually means an upstream merge relocated the anchor and the fork "
        f"behaviour is now silently disabled. Re-wire it; do not delete this check."
    )


def test_fork_toolsets_are_registered():
    """The fork's four integrations plus ownership must exist as their own
    toolsets, so RBAC can gate each independently."""
    import toolsets

    all_toolsets = set(toolsets.get_all_toolsets())
    for name in ("notion", "jira", "slack_post", "webflow_assets", "ownership", "slack"):
        assert name in all_toolsets, (
            f"toolset '{name}' is not registered — RBAC cannot gate it, and any "
            f"role granting it becomes a no-op."
        )


def test_floor_toolsets_reach_valid_role_users_only():
    """Floors are granted at the enforcement surface, never to roleless users."""
    from gateway.tool_access import FLOOR_TOOLSETS, policy_from_extra

    assert {"clarify", "todo", "slack", "ownership"} <= set(FLOOR_TOOLSETS)

    policy = policy_from_extra({"user_roles": {"U_OP": "operator"}})
    assert policy.enabled
    for floor in ("ownership", "slack", "todo", "clarify"):
        assert policy.can_use_tool("U_OP", floor), f"floor {floor} denied to a valid role"
        assert not policy.can_use_tool("U_NOBODY", floor), (
            f"floor {floor} leaked to a roleless user — deny-until-assigned is broken"
        )


def test_operator_role_still_excludes_terminal():
    """The operator role must never reach a host shell: `printenv` / `cat
    ~/.hermes/.env` would hand over every credential on the box."""
    from gateway.tool_access import policy_from_extra

    policy = policy_from_extra({"user_roles": {"U_OP": "operator"}})
    assert not policy.can_use_tool("U_OP", "terminal")
    assert not policy.can_use_tool("U_OP", "code_execution")
