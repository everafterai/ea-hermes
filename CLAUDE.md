# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## This is a fork

This repo is **`everafterai/ea-hermes`** — a fork of NousResearch's
[hermes-agent](https://github.com/NousResearch/hermes-agent), customized for
EverAfter's needs. It is **deployed to a Google Cloud VM** (the gateway runs there,
serving Slack and other messaging platforms).

When working here:
- **[AGENTS.md](AGENTS.md) is the canonical, upstream development guide** — project
  structure, the `AIAgent` loop, CLI/TUI architecture, tools/toolsets, plugins,
  skills, cron, kanban, profiles, prompt-caching invariants, and the full list of
  "Known Pitfalls". Read it; this file does not duplicate it.
- This file documents only what is **fork-specific** or most load-bearing day to day.
- Prefer the plugin route for new local-only tools (see AGENTS.md "Adding New Tools")
  rather than editing core — it keeps merges with upstream clean.

## Fork-specific work: Slack per-user RBAC + multi-user session isolation

The fork's primary divergence from upstream is **role-based access control (RBAC)
over tool execution for messaging-platform users (primarily Slack)**, plus the
**multi-user session isolation** fixes that go with running one gateway for many users.

### RBAC policy module — [gateway/tool_access.py](gateway/tool_access.py)

A pure, unit-testable `ToolAccessPolicy` that sits beside the chat allowlist
([gateway/run.py](gateway/run.py) `_is_user_authorized`) and the slash-command
tiers ([gateway/slash_access.py](gateway/slash_access.py)) and adds a third axis:
**which toolsets an identified platform user may invoke**, keyed to named roles.

- **Built-in roles** (`BUILTIN_ROLES`): `admin` (`*`), `operator`, `readonly`,
  `chat_only` (no tools). `FLOOR_TOOLSETS` (`clarify`, `todo`, `slack`) are granted
  to every valid-role user — but NOT to roleless/undefined-role users. `slack`
  (`slack_react` + `turn_end`) is a floor because reacting/closing a turn is UX,
  not a privilege — so the bot can acknowledge any user's message in a quiet
  channel, not just an admin's.
- **Config lives under the top-level `slack:` block** in `~/.hermes/config.yaml`
  (`user_roles`, optional `user_names`, optional `roles`, optional `channel_roles`).
  The gateway config loader ([gateway/config.py](gateway/config.py)) bridges these
  keys into the platform's runtime `extra`. **Hand-edit under `slack:` directly,
  NOT `slack.extra`.**
- **`channel_roles` (`{chat_id: role}`, Slack-only)** grants a fixed *service
  role* to EVERY poster in that channel, so a channel (e.g. issue-tracking) works
  no matter who reports — even a roleless user. The channel grant is **UNIONed**
  with the poster's own role (additive; never reduces access) and is resolved at
  all three enforcement points via the chat id (`ToolAccessPolicy._effective_grant`;
  the backstop reads `HERMES_SESSION_CHAT_ID` via `_current_chat_id`). It is
  **inert unless RBAC is active** — it does NOT by itself enable RBAC (activation
  stays keyed to `user_roles`). **Security:** anyone who can post in such a channel
  can invoke that role's tools (e.g. `terminal`), so scope the role to least
  privilege and keep the channels internal.
- **Activation is `user_roles` presence.** When `user_roles` is non-empty, RBAC is
  the sole authorization source — a user with a role may chat and gets that role's
  toolsets; a roleless user is denied entirely (deny-until-assigned). It also
  short-circuits/retires `SLACK_ALLOWED_USERS` and ignores `SLACK_ALLOW_ALL_USERS`.
- **Backward compatible / fail-closed:** absent `user_roles` → policy disabled,
  upstream behavior exactly. A `user_roles` entry naming an undefined role denies
  that user and logs.
- **Three enforcement points, one policy:** (A) message gate in `gateway/run.py`,
  (B) toolset filter feeding `enabled_toolsets` into `model_tools.get_tool_definitions`
  (cache-safe — `enabled_toolsets` is in the memo key), (C) execution backstop in
  the `pre_tool_call` hook in [model_tools.py](model_tools.py) that resolves
  identity from session contextvars and hard-blocks forbidden tools (covers
  delegation sub-agents, the code-execution sandbox, plugin-invoked calls).

Design + plan docs (read these before changing RBAC):
[docs/superpowers/specs/2026-05-31-slack-tool-rbac-design.md](docs/superpowers/specs/2026-05-31-slack-tool-rbac-design.md)
and [docs/superpowers/plans/2026-05-31-slack-tool-rbac.md](docs/superpowers/plans/2026-05-31-slack-tool-rbac.md).

### `hermes users` CLI — [hermes_cli/users.py](hermes_cli/users.py)

Manages RBAC users with comment-preserving writes to `config.yaml`:
`hermes users list | add <id> <role> [--name] | update <id> [--role] [--name] | delete <id>`.
Promoting a user to `admin` keeps `allow_admin_from` (the slash-admin list) in sync;
demoting/deleting removes them.

### `hermes tools rbac` — lists tools by toolset with built-in role coverage.

### Session visibility / multi-user isolation — [hermes_state.py](hermes_state.py)

`SessionDB` gained `chat_id`/`chat_type` columns and visibility helpers so
`session_search` and `/resume` scope to the requesting identity (a series of
cross-user-leak security fixes). DM scope is strictly partitioned from channel
sessions. `chat_type` propagates via session contextvars
([gateway/session_context.py](gateway/session_context.py)). Owner identity is
inherited into compression + branch sessions to preserve self-recall. When
touching session search/resume/scoping, preserve these isolation guarantees — they
have end-to-end tests under [tests/](tests/) (e.g. multi-user `session_search` isolation).

### Slack quiet channels + `slack_react`

For low-noise "hidden assistant" channels. Config under the top-level `slack:`
block in `~/.hermes/config.yaml`:

- `quiet_channels: 'C123,C456'` — comma-separated channel IDs (mirrors
  `free_response_channels`). In these channels the gateway forces
  `tool_progress: off` and allows **silent (emoji-only) completion**: a
  successful turn that produces no text stays silent instead of posting the
  "no response generated" warning. Resolved in [gateway/run.py](gateway/run.py)
  via `_is_quiet_channel` (matches `chat_id` or thread `parent_chat_id`).
  Errors, approvals, and clarifications still surface. **Text replies are never
  suppressed** — the bot can always answer.
- `slack_react` tool ([tools/slack_react_tool.py](tools/slack_react_tool.py)) —
  lets the agent add/remove an emoji reaction on the triggering Slack message
  (or an explicit `message_id`). Targets the message via session contextvars
  (`HERMES_SESSION_MESSAGE_ID`, set in [slack.py](gateway/platforms/slack.py)
  `build_source(message_id=ts)`). Lives in the platform-restricted `slack`
  toolset; grant it to a role via `slack.roles`. Emoji-first behavior is driven
  by `channel_prompts`, not enforced.
- `turn_end` tool ([tools/slack_react_tool.py](tools/slack_react_tool.py)) —
  a **terminal** tool: the agent calls it as its final action (e.g. after
  `slack_react`) to finish a turn silently with no text. The conversation loop
  ([agent/conversation_loop.py](agent/conversation_loop.py)
  `_called_terminal_turn_end`) ends the turn on this tool call **only when
  `_silent_completion_ok` is set** — the gateway sets that per-turn for quiet
  channels. Ending on a tool call (not an empty text response) is what avoids
  the loop's empty-response recovery nets (post-tool nudge / retry / fallback)
  that would otherwise force unwanted text. Inert (benign no-op ack) outside
  quiet channels. Drive it via `channel_prompts`: "react, then call `turn_end`."
  Safety net: even when the model forgets `turn_end`, an empty/no-content turn in
  a quiet channel is accepted silently (`_should_accept_silent_empty` in
  [agent/conversation_loop.py](agent/conversation_loop.py)) — the empty-response
  recovery (prior-content fallback / nudge / retry / provider fallback) is
  skipped when `_silent_completion_ok` is set. Thinking-only responses still
  prefill-continue. **Text replies are never suppressed** — only empty turns.
  Same for the codex/Responses path: a gpt-5.x turn that yields only reasoning
  (no text, no tool call) is classified `incomplete` and continued up to 3×; when
  that's exhausted in a quiet channel, `_codex_incomplete_exhausted_result`
  ([agent/conversation_loop.py](agent/conversation_loop.py)) finishes silently
  instead of posting "Codex response remained incomplete…" (the model chose to
  stay silent). Non-quiet channels keep the warning.
- **Relevance pre-gate** (default for `quiet_channels`): before the full agent
  runs, a cheap classifier (`slack.relevance_gate_model`, empty = main model)
  decides act/ignore on each **non-@mention** message; `ignore` ends the turn
  with no agent run. Lives in [gateway/run.py](gateway/run.py)
  (`_relevance_gate_should_skip` → `_classify_relevance` via `async_call_llm`),
  invoked in `_handle_message` after `pre_gateway_dispatch`. @mention/DM bypass
  it (`MessageEvent.directly_addressed`, set in [slack.py](gateway/platforms/slack.py)).
  Purpose per channel: `slack.relevance_gate_purpose[chat_id]` →
  `channel_prompts[chat_id]` → a generic default. **Fail-open**: classifier
  error → the agent runs (never silently drops a real message). Only active on
  Slack quiet channels; inert elsewhere.

### Per-channel / per-task model overrides

Route work to different models without changing the global `model.default`.
All surfaces share one entry shape — a model string OR
`{model, provider, base_url}` (credentials always resolved host-side; never
put API keys in entries). Shared helpers: [agent/model_override.py](agent/model_override.py).
Design doc: [docs/superpowers/specs/2026-06-10-model-overrides-design.md](docs/superpowers/specs/2026-06-10-model-overrides-design.md).

- **`slack.channel_models`** (top-level `slack:` block): `chat_id → entry`;
  threads inherit via `parent_chat_id`. Applied in
  `_apply_channel_model_override` ([gateway/run.py](gateway/run.py)).
  Precedence: session `/model` > `channel_models` > global default.
  Fail-open — a bad entry logs and falls back to the global model.
- **Skill frontmatter** (`metadata.hermes.{model,provider,base_url}` in
  SKILL.md): invoking the skill (slash command or channel auto-skill)
  switches the session's model for the rest of the session, exactly like
  `/model` (writes `_session_model_overrides`, evicts the cached agent).
  Last writer wins; cleared by `/new`/`/reset`/restart. CLI sessions, skill
  bundles, and plugin skills are out of scope (v1).
- **Cron**: jobs already carry per-job `model`/`provider`/`base_url`;
  additionally a skill listed in `job.skills` fills any of those fields the
  job leaves unset (`_effective_job_model_fields` in
  [cron/scheduler.py](cron/scheduler.py)). Job fields always win.
- **Delegation**: `delegate_task` accepts `model`/`provider`/`base_url`
  (top-level and per-task) so sub-agents can run on cheaper/stronger models;
  omitted = inherit parent ([tools/delegate_tool.py](tools/delegate_tool.py)).

## Common commands

```bash
# Setup (fork dev)
./setup-hermes.sh                         # installs uv, venv, .[all], symlinks hermes
source .venv/bin/activate                 # or: source venv/bin/activate

# Tests — ALWAYS use the wrapper, never bare pytest (CI-parity: unset creds, TZ=UTC,
# C.UTF-8, xdist, per-test subprocess isolation). See AGENTS.md "Testing".
scripts/run_tests.sh                                   # full suite
scripts/run_tests.sh tests/gateway/                    # one directory
scripts/run_tests.sh tests/gateway/test_tool_access.py # one file
scripts/run_tests.sh tests/gateway/test_tool_access.py::test_x  # one test
scripts/run_tests.sh --no-isolate tests/foo/           # faster, for debugging
scripts/run_tests.sh -v --tb=long                      # pass-through pytest flags

# Lint / typecheck (ruff is intentionally near-disabled — only PLW1514 is enforced)
ruff check .
ty check                                  # type checker (configured in pyproject.toml)

# Run the agent locally
hermes                                    # interactive CLI
hermes --tui                              # Ink/React TUI (or HERMES_TUI=1)
hermes gateway                            # messaging gateway (Slack, etc.)
hermes doctor                             # diagnose issues

# TUI dev (see AGENTS.md "TUI Architecture")
cd ui-tui && npm run dev                  # watch mode
```

## Key invariants (do not break)

- **Prompt caching:** never alter past context, swap toolsets, or rebuild system
  prompts mid-conversation (only context compression may). Cache-mutating slash
  commands default to deferred invalidation with an opt-in `--now`. (AGENTS.md)
- **Profiles:** use `get_hermes_home()` / `display_hermes_home()` from
  `hermes_constants` — never hardcode `~/.hermes`. (AGENTS.md)
- **Dependency pinning:** direct deps are exact-pinned in
  [pyproject.toml](pyproject.toml); regenerate `uv.lock` with `uv lock` on changes.
  Provider/backend-specific deps go in extras + lazy-install, not core. (AGENTS.md)
- **Tests must not write to `~/.hermes/`** (autouse fixture redirects `HERMES_HOME`);
  don't write change-detector tests. (AGENTS.md)
- **RBAC fail-closed:** when editing the policy or its enforcement points, keep the
  deny-until-assigned semantics and the activation boundary (empty `user_roles` =
  upstream behavior) intact.
