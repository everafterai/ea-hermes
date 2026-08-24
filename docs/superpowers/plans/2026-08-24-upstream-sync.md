# Upstream Sync 2026-08-24 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge `upstream/main` (NousResearch/hermes-agent, v0.20.5) into the fork,
carrying every fork feature forward intact — RBAC, multi-user session isolation,
cross-user data protection, automation ownership, the cron toolset ceiling, quiet
channels, model overrides, and the four fork-only toolsets.

**Non-goal:** Adopting new upstream features into fork config. This merge restores
parity; enabling voice, smart approvals, scale-to-zero, etc. is separate work.

## Baseline facts (measured 2026-08-24)

| | |
|---|---|
| Merge base | `a317e5493` (2026-06-07) |
| Upstream ahead | 14,192 commits · 8,915 files · +1,736,980 / −433,343 |
| Fork ahead | 210 commits · 103 new files |
| Version | 0.16.0 → 0.20.5 |
| Dry-run merge | **41 conflicted files, ~136 hunks, 1 modify/delete** |

Branch: `chore/sync-upstream-2026-08-24`. `rerere` + `zdiff3` conflict style enabled
so a re-run of the merge replays resolutions.

## Architecture of the risk

Conflicts are the *visible* cost and they are tractable. The real risk is the
**silent** kind: fork code that merges cleanly but no longer runs because upstream
moved the call site out from under it. Five such relocations were found by grepping
every fork integration anchor against `upstream/main`. Each gets its own task below.

Anchors verified **still present** upstream (no action needed): `handle_function_call`
(RBAC enforcement point C), `_resolve_cron_enabled_toolsets`, `pre_gateway_dispatch`,
`platform_toolsets.<platform>` resolution in `gateway/platforms/base.py`,
`gateway/slash_access.py`, `gateway/session_context.py`, `toolsets.py`,
`agent/system_prompt.py`, `agent/file_safety.py`.

## Verification gate

The 56 fork-added test files under `tests/` are the regression net. The merge is not
done until they pass. Listed in `docs/superpowers/plans/2026-08-24-upstream-sync-tests.txt`.

```bash
scripts/run_tests.sh $(cat docs/superpowers/plans/2026-08-24-upstream-sync-tests.txt)
```

Full-suite baseline was captured on the pre-merge branch so post-merge failures can
be attributed.

---

## Phase 0 — Preparation

- [x] Fetch `upstream/main` (needs `git -c credential.helper='!gh auth git-credential'`;
      anonymous fetches are HTTP 429'd by GitHub)
- [x] Clean the working tree — `.gitignore` had 4 em-dashes corrupted to `???` by an
      editor; reverted, kept only the intentional `config.yaml` line
- [x] Branch `chore/sync-upstream-2026-08-24`, enable `rerere` + `zdiff3`
- [x] Capture full-suite baseline
- [ ] Record the conflict inventory and fork-test list alongside this plan

## Phase 1 — The merge

- [ ] `git merge upstream/main` and resolve all ~136 hunks
- [ ] Resolution rule: **upstream wins on structure, fork wins on policy.** Take
      upstream's refactors, re-apply fork behavior on top. Never resolve by deleting
      a fork guard to make a conflict go away.
- [ ] `ruff check .` and `ty check` clean before committing the merge

### Conflict inventory (hunks per file)

Source (27): `gateway/run.py` 20 · `tools/session_search_tool.py` 11 ·
`tools/image_generation_tool.py` 9 · `tools/file_tools.py` 9 · `tools/cronjob_tools.py` 8 ·
`tools/skill_manager_tool.py` 7 · `cron/scheduler.py` 7 · `hermes_state.py` 5 ·
`tools/memory_tool.py` 4 · `tools/delegate_tool.py` 4 · `tools/approval.py` 4 ·
`hermes_cli/main.py` 3 · `gateway/session.py` 3 · `agent/conversation_loop.py` 3 ·
`plugins/memory/holographic/__init__.py` 2 · then 1 each: `agent/agent_init.py`,
`agent/background_review.py`, `agent/conversation_compression.py`, `agent/display.py`,
`agent/redact.py`, `gateway/config.py`, `gateway/platforms/base.py`,
`hermes_cli/config.py`, `hermes_cli/tools_config.py`, `plugins/image_gen/openai/__init__.py`,
`tools/skills_tool.py` · plus `gateway/platforms/slack.py` (modify/delete)

Tests (13): `test_resume_command.py` 8 · `test_slack.py` 3 · `test_image_generation.py` 2 ·
`test_session_boundary_security_state.py` 2 · `tests/gateway/test_config.py` 2 · 1 each:
`test_image_generation_artifacts.py`, `test_live_system_guard_self_test.py`,
`test_openai_provider.py`, `tests/hermes_cli/test_config.py`, `test_home_target_env_var.py`,
`tests/cron/test_scheduler.py`, `tests/conftest.py`, `tests/agent/test_redact.py`

Docs (1): `website/docs/user-guide/features/image-generation.md`

## Phase 2 — The five relocations

### 2.1 Slack platform → bundled plugin (highest risk)

Upstream **deleted** `gateway/platforms/slack.py`; Slack is now
`plugins/platforms/slack/adapter.py` (9,612 lines). Feishu and WeCom moved the same
way. Git surfaces this as modify/delete, so it will not vanish silently — but
resolving it wrong leaves a dead file and a Slack adapter with none of the fork's
behavior.

The fork's 58 added lines to port:
- [ ] `_slack_quiet_channels()` — reads `slack.quiet_channels` from `config.extra`
- [ ] quiet-channel checks at the two message-handling sites
- [ ] `directly_addressed=bool(is_dm or is_mentioned)` on the built source (fork-only
      field; drives the relevance pre-gate bypass)
- [ ] `_anchor_message_author()` and its call site
- [ ] **Drop** the `message_id=ts` patch — upstream's `build_source()` now takes
      `message_id` natively, which is exactly the fix the fork carries for `slack_react`
- [ ] Delete the stale `gateway/platforms/slack.py`
- [ ] Confirm `platform_toolsets.slack` still gates the adapter (resolution stayed in
      `gateway/platforms/base.py`, which the plugin inherits) — this is the known
      deployment landmine for `ownership` / `webflow_assets` / `slack_post`

The port is near-1:1: the plugin keeps the same `self.config.extra.get(...)` pattern
and `_slack_free_response_channels()` helper the fork mirrors.

### 2.2 RBAC enforcement point A → `gateway/authz_mixin.py`

Upstream extracted the authorization cluster into a mixin; `def _is_user_authorized`
no longer exists in `gateway/run.py`, only its call sites. The fork's RBAC gate block
lands in a conflict region orphaned from its function.

- [ ] Move the RBAC block (`policy_for_source` / `_load_config_cached` / the
      `SLACK_ALLOWED_USERS`-is-ignored warning / `is_authorized(user_id, chat_id)`)
      into `_is_user_authorized` in `gateway/authz_mixin.py`
- [ ] Keep it **first** in the function — RBAC is the sole authorization source when
      active, overriding env allowlists, allow-all flags, and DM pairing
- [ ] Verify `tests/gateway/test_tool_access_enforcement.py` still targets the right
      seam; retarget if it patched `run.py` directly

### 2.3 `hermes_state.py` — converged schema

Upstream independently added `chat_id`/`chat_type` columns to `sessions`, including
`COALESCE`-based upsert handling. This is **reconciliation, not re-application**.

- [ ] Adopt upstream's columns and upsert; do not re-add fork duplicates
- [ ] Keep the fork's `build_scope`, `build_visibility_where`, `session_row_visible`,
      `backfill_session_scope` on top of upstream's schema
- [ ] Confirm DM scope stays strictly partitioned from channel sessions
- [ ] Gate: `tests/hermes_state/test_visibility_scope.py`,
      `tests/gateway/test_resume_user_isolation.py`

### 2.4 `tools/session_search_tool.py` — rewritten around the fork's guard

828 → 1,321 lines upstream, 11 conflict hunks, and it is a **security boundary**
(cross-user session leakage). Upstream added its own lineage/visibility concepts
(`_resolve_lineage`, `_session_left_live_context`, `_is_compaction_summary`) that the
fork's scope gate must compose with, not fight.

- [ ] Re-apply the scope gate to every read shape, not just `discover` — the June sync
      shipped a regression here (`65e153000`, "scope-gate the session_search READ shape")
- [ ] Enumerate upstream's read paths (`_read_session`, `_list_recent_sessions`,
      `_scroll`, `_discover`, `_title_match_result`) and confirm each is gated
- [ ] Gate: `tests/tools/test_session_search_isolation_e2e.py`,
      `tests/tools/test_session_search_scope.py`

### 2.5 `tools/approval.py` — 3× bigger upstream

1,872 → 5,703 lines (upstream's "smart approvals": LLM reviewer, `approvals.suggest`,
`smart_policy`, consecutive-denial circuit breaker). The fork's
`approvals.require_for_tools` — a deterministic per-tool gate — has **no upstream
equivalent** and must survive.

- [ ] Re-apply `require_for_tools` onto the new structure
- [ ] Check whether upstream's smart-approval path can bypass the deterministic gate;
      if so, the fork gate must run first and fail closed
- [ ] Gate: `tests/tools/test_tool_approval.py`,
      `tests/tools/test_tool_approval_dispatch.py`,
      `tests/cron/test_tool_approval_context.py`

## Phase 3 — Verification

- [ ] Fork regression net passes (56 files)
- [ ] Full suite passes, diffed against the pre-merge baseline; every new failure
      either fixed or explicitly attributed to upstream
- [ ] `ruff check .` and `ty check` clean
- [ ] `hermes doctor` runs
- [ ] Manual smoke on the gateway: a Slack DM from an RBAC-roled user, a quiet-channel
      message (emoji-only completion), a `terminal` denial for an `operator`

## Phase 4 — Documentation

- [ ] Update `CLAUDE.md` for the Slack plugin move — every `gateway/platforms/slack.py`
      reference is now `plugins/platforms/slack/adapter.py`
- [ ] Update `CLAUDE.md` for the `authz_mixin.py` relocation of enforcement point A
- [ ] Note in `CLAUDE.md` that `build_source(message_id=)` is upstream now
- [ ] Verify every file path cited in `CLAUDE.md` still exists post-merge

## Phase 5 — Deployment

- [ ] Do **not** run `hermes update` on the VM during this work — it autostashes
      uncommitted changes and switches to main
- [ ] Core deps roughly doubled (79 → 146 dependency lines); `requires-python` is
      unchanged at `>=3.11,<3.14`. The VM needs a dependency install, not just a pull.
- [ ] Config review before restart: upstream shipped a config-migration wave
      (verify-on-stop v32 migration, managed scope, MCP 2.x SDK)
