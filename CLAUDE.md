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
  `chat_only` (no tools). `FLOOR_TOOLSETS` (`clarify`, `todo`) are granted to every
  valid-role user — but NOT to roleless/undefined-role users.
- **Config lives under the top-level `slack:` block** in `~/.hermes/config.yaml`
  (`user_roles`, optional `user_names`, optional `roles`). The gateway config
  loader ([gateway/config.py](gateway/config.py)) bridges these keys into the
  platform's runtime `extra`. **Hand-edit under `slack:` directly, NOT `slack.extra`.**
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
  (or an explicit `message_id`). Lives in the platform-restricted `slack`
  toolset; grant it to a role via `slack.roles`. Emoji-first behavior is driven
  by `channel_prompts`, not enforced.

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
