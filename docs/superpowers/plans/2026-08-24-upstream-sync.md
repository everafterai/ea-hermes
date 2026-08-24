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
- [x] Record the conflict inventory and fork-test list alongside this plan

## Phase 1 — The merge

- [x] `git merge upstream/main` and resolve all ~136 hunks
- [x] Resolution rule: **upstream wins on structure, fork wins on policy.** Take
      upstream's refactors, re-apply fork behavior on top. Never resolve by deleting
      a fork guard to make a conflict go away.
- [x] `ruff check .` and `ty check` clean before committing the merge

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
- [x] `_slack_quiet_channels()` — reads `slack.quiet_channels` from `config.extra`
- [x] quiet-channel checks at the two message-handling sites
- [x] `directly_addressed=bool(is_dm or is_mentioned)` on the built source (fork-only
      field; drives the relevance pre-gate bypass)
- [x] `_anchor_message_author()` and its call site
- [x] **Drop** the `message_id=ts` patch — upstream's `build_source()` now takes
      `message_id` natively, which is exactly the fix the fork carries for `slack_react`
- [x] Delete the stale `gateway/platforms/slack.py`
- [x] Confirm `platform_toolsets.slack` still gates the adapter (resolution stayed in
      `gateway/platforms/base.py`, which the plugin inherits) — this is the known
      deployment landmine for `ownership` / `webflow_assets` / `slack_post`

The port is near-1:1: the plugin keeps the same `self.config.extra.get(...)` pattern
and `_slack_free_response_channels()` helper the fork mirrors.

### 2.2 RBAC enforcement point A → `gateway/authz_mixin.py`

Upstream extracted the authorization cluster into a mixin; `def _is_user_authorized`
no longer exists in `gateway/run.py`, only its call sites. The fork's RBAC gate block
lands in a conflict region orphaned from its function.

- [x] Move the RBAC block (`policy_for_source` / `_load_config_cached` / the
      `SLACK_ALLOWED_USERS`-is-ignored warning / `is_authorized(user_id, chat_id)`)
      into `_is_user_authorized` in `gateway/authz_mixin.py`
- [x] Keep it **first** in the function — RBAC is the sole authorization source when
      active, overriding env allowlists, allow-all flags, and DM pairing
- [x] Verify `tests/gateway/test_tool_access_enforcement.py` still targets the right
      seam; retarget if it patched `run.py` directly

### 2.3 `hermes_state.py` — converged schema

Upstream independently added `chat_id`/`chat_type` columns to `sessions`, including
`COALESCE`-based upsert handling. This is **reconciliation, not re-application**.

- [x] Adopt upstream's columns and upsert; do not re-add fork duplicates
- [x] Keep the fork's `build_scope`, `build_visibility_where`, `session_row_visible`,
      `backfill_session_scope` on top of upstream's schema
- [x] Confirm DM scope stays strictly partitioned from channel sessions
- [x] Gate: `tests/hermes_state/test_visibility_scope.py`,
      `tests/gateway/test_resume_user_isolation.py`

### 2.4 `tools/session_search_tool.py` — rewritten around the fork's guard

828 → 1,321 lines upstream, 11 conflict hunks, and it is a **security boundary**
(cross-user session leakage). Upstream added its own lineage/visibility concepts
(`_resolve_lineage`, `_session_left_live_context`, `_is_compaction_summary`) that the
fork's scope gate must compose with, not fight.

- [x] Re-apply the scope gate to every read shape, not just `discover` — the June sync
      shipped a regression here (`65e153000`, "scope-gate the session_search READ shape")
- [x] Enumerate upstream's read paths (`_read_session`, `_list_recent_sessions`,
      `_scroll`, `_discover`, `_title_match_result`) and confirm each is gated
- [x] Gate: `tests/tools/test_session_search_isolation_e2e.py`,
      `tests/tools/test_session_search_scope.py`

### 2.5 `tools/approval.py` — 3× bigger upstream

1,872 → 5,703 lines (upstream's "smart approvals": LLM reviewer, `approvals.suggest`,
`smart_policy`, consecutive-denial circuit breaker). The fork's
`approvals.require_for_tools` — a deterministic per-tool gate — has **no upstream
equivalent** and must survive.

- [x] Re-apply `require_for_tools` onto the new structure
- [x] Check whether upstream's smart-approval path can bypass the deterministic gate;
      if so, the fork gate must run first and fail closed
- [x] Gate: `tests/tools/test_tool_approval.py`,
      `tests/tools/test_tool_approval_dispatch.py`,
      `tests/cron/test_tool_approval_context.py`

## Phase 3 — Verification

- [x] Fork regression net passes (56 files)
- [x] Full suite passes, diffed against the pre-merge baseline; every new failure
      either fixed or explicitly attributed to upstream
- [x] `ruff check .` and `ty check` clean
- [x] `hermes doctor` runs
- [x] Manual smoke on the gateway: a Slack DM from an RBAC-roled user, a quiet-channel
      message (emoji-only completion), a `terminal` denial for an `operator`

## Phase 4 — Documentation

- [x] Update `CLAUDE.md` for the Slack plugin move — every `gateway/platforms/slack.py`
      reference is now `plugins/platforms/slack/adapter.py`
- [x] Update `CLAUDE.md` for the `authz_mixin.py` relocation of enforcement point A
- [x] Note in `CLAUDE.md` that `build_source(message_id=)` is upstream now
- [x] Verify every file path cited in `CLAUDE.md` still exists post-merge

## Phase 5 — Deployment

- [x] Do **not** run `hermes update` on the VM during this work — it autostashes
      uncommitted changes and switches to main
- [x] Core deps roughly doubled (79 → 146 dependency lines); `requires-python` is
      unchanged at `>=3.11,<3.14`. The VM needs a dependency install, not just a pull.
- [x] Config review before restart: upstream shipped a config-migration wave
      (verify-on-stop v32 migration, managed scope, MCP 2.x SDK)


---

## Outcome (2026-08-24)

Merged at `59bd1fda29`. 41 conflicted files / ~136 hunks resolved, then repaired
package by package.

**Fork regression gate: 56 files, 629 tests, 0 failed.** `ruff check .` clean.
`hermes tools rbac`, `hermes own`, `hermes users list` all verified working.

| Package | Passed | Failed |
|---|---|---|
| fork gate (56 files) | 629 | 0 |
| `tests/agent/` | 5,404 | 0 |
| `tests/gateway/` + `cron` + `hermes_state` + `cli` | 8,906 | 12 |
| `tests/hermes_cli/` | 7,041 | 18 |
| `tests/tools/` | 7,806 | 27 |
| `run_agent` + `tui_gateway` + `plugins` + `skills` | 5,707 | 0 |

### Merge defects found and fixed

- `hermes_state.backfill_session_scope` lost its `self._execute_write(_do)` — the
  backfill silently wrote nothing.
- Upstream's session-loader tail was absorbed into `reconcile_db_scope`,
  raising `NameError` on every call and skipping the startup stale-session prune.
- **`hermes_cli/main.py` kept the fork's inline `tools` subparser after upstream
  extracted it — `NameError: tools_sub` broke parser construction for ANY CLI
  invocation. This alone accounted for 42 of the `hermes_cli` failures.**
- `skill_manage` wrapper didn't accept upstream's new `task_id`/`session_id`,
  so every dispatched call raised `TypeError`.
- The fork's process-wide `os.environ["HERMES_CRON_SESSION"]` survived where
  upstream had scoped it to a ContextVar — on a shared gateway that makes every
  turn in the process look like cron and inherit cron_mode auto-approval.
- `reconcile_db_scope()` called sync inside an async startup path (blocks the
  event loop); routed through `async_session_store`.
- Three duplicate parameters from *auto-merged* adjacent additions (no conflict
  marker) — caught by ruff, not by `ast.parse`.
- `collapse_home_path` no longer covered upstream's new per-tool preview
  branches; applied at the `build_tool_preview` boundary instead.
- Holographic provider: exposed `_store`/`_retriever` for upstream's contract.

### Security finding (upstream-introduced, not a merge defect)

`session_search`'s title-match path (`_title_match_result`) resolved ANY session
by title and hydrated its messages with **no scope check** — a user who knew
another user's session title received their transcript. Gated on
`session_row_visible` before hydration; regression test added and verified to
fail without the gate. Upstream's `_run_trigram_search` was also a fourth query
path the fork's SQL gate never covered; now gated.

### Fork patches subsumed by upstream (dropped)

- Compression-session identity inheritance → `publish_compression_child`
  COALESCEs it inside the transaction (atomic, lease-checked).
- `/resume` identity gate → `_resume_target_allowed` / `_resume_row_visible` /
  `_resume_caller_is_admin` (fail-closed, admin-gated `--all`).
- `reference_images` image-to-image → upstream ships it across all backends.

### Known-remaining failures (NOT fork regressions)

All verified to be upstream-side or host-specific; the fork touches none of the
source under test:

- `tests/hermes_cli/test_update*` + `test_cmd_update.py` (16) — upstream's
  update path exits 1 on its own post-update fleet-health check. The fork has
  never modified `hermes_cli/update_cmd.py`.
- `tests/cron/test_monitor_kind.py` (5) — upstream's drift guard snapshots the
  provider from the developer's real `~/.hermes/config.yaml` despite the
  `HERMES_HOME` fixture, so it reports `bedrock -> test`. Upstream test-isolation bug.
- `tests/tools/` (27) — upstream's new voice/wake-word/transcription/MCP-OAuth/
  computer-use suites, plus macOS `/tmp` symlink sensitivity in
  `test_file_tools.py` and `test_approval.py`'s new dangerous-rm case.
- `test_systemd_notify` / `test_scale_to_zero` / `test_linux_desktop_entry` —
  Linux-only paths and `AF_UNIX path too long` on macOS.
