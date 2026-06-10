# Per-channel and per-task model overrides — design

**Date:** 2026-06-10
**Status:** Implemented

## Problem

The gateway runs one global model (`model.default` in `config.yaml`); the only
override is the per-session `/model` command. We want to route work to
different models by **Slack channel** and by **task**: a cheap model for a
triage channel or recurring cron job, a strong model for a research skill,
cheap leaf workers under delegation.

## The shared entry shape

Every surface uses the same "model override entry":

- a plain model string — `"gpt-5-mini"` — resolved against the currently
  configured provider/credentials, **or**
- a dict `{model, provider, base_url}` (any subset) — when `provider` or
  `base_url` is set, credentials are resolved host-side via
  `hermes_cli.runtime_provider.resolve_runtime_provider(requested=…,
  explicit_base_url=…, target_model=…)` — the exact pattern cron jobs already
  use. API keys are **never** written into entries.

Normalization/resolution lives in **`agent/model_override.py`**
(`normalize_model_override`, `extract_skill_model_override`,
`resolve_override_runtime`) — pure, lazily importing, shared by all surfaces.

## Surfaces

### 1. `slack.channel_models` (per-channel, Slack only)

```yaml
slack:
  channel_models:
    "C0123TRIAGE": gpt-5-mini
    "C0456RESEARCH":
      model: claude-opus-4
      provider: anthropic
```

- Bridged into the Slack platform `extra` in `gateway/config.py` (keys
  stringified, like `channel_prompts`). Slack-only by design.
- Resolved per message by `resolve_channel_model` in
  `gateway/platforms/base.py`: exact `chat_id` match, then `parent_chat_id`
  (threads inherit the channel's model).
- Applied in `GatewayRunner._apply_channel_model_override`, called from
  `_resolve_session_agent_runtime` **after** runtime-provider resolution and
  **before** `_apply_session_model_override` — so precedence falls out of
  position.
- **Fail-open:** any lookup/credential problem logs a warning and leaves the
  global resolution untouched; a misconfigured entry never breaks a channel.

### 2. Skill frontmatter (per-skill)

```yaml
---
name: deep-research
description: …
metadata:
  hermes:
    model: claude-opus-4
    provider: anthropic       # optional
---
```

- `metadata.hermes.{model,provider,base_url}` is canonical (merge-safe,
  agentskills.io-compatible); top-level keys are accepted as a fallback. No
  per-field mixing between the two sources.
- `skill_view` exposes the parsed entry as `model_override` in its result.
- **Semantics: switch for the rest of the session, exactly like `/model`** —
  `GatewayRunner._apply_skill_model_override` writes the resolved bundle into
  `_session_model_overrides[session_key]`, evicts the cached agent, and queues
  a pending model note. Because skill interception happens before the turn's
  agent runtime is resolved, the skill's own turn already runs on the new
  model.
- Wired paths: gateway slash-skill invocation
  (`_apply_skill_model_override_from_path` reading SKILL.md frontmatter),
  channel auto-skills (`channel_skill_bindings`, first override-bearing skill
  wins, applied at session start), and cron (`job.skills` — see below).
- Repeat invocations of the same skill are no-ops (no cache churn). Last
  writer wins: `/model` after a skill overrides it and vice versa. Overrides
  die on `/new`/`/reset` and on gateway restart (in-memory, same as `/model`).
- **Out of scope (v1):** CLI sessions (use `/model`), skill bundles, plugin
  skills (`namespace:skill` via `_serve_plugin_skill`).

### 3. Cron jobs (already existed) + skill interplay

Cron jobs already carry per-job `model` / `provider` / `base_url`. New:
`_effective_job_model_fields(job)` in `cron/scheduler.py` merges, **field-wise**,
`job.* > first skill-declared override (from job.skills frontmatter) > config
default`. A skill probe is one extra cheap `skill_view(name, preprocess=False)`
call per run; failures are fail-open.

### 4. Delegate tool (per sub-agent)

`delegate_task` accepts `model`, `provider`, `base_url` at top level and per
task. Field-wise precedence: per-task > top-level params > `delegation.*`
config > inherit parent. Provider/base_url entries resolve credentials via
`resolve_override_runtime` (cached per batch); resolution failure returns a
`tool_error` before any child is built. RBAC is unaffected (the
`pre_tool_call` backstop gates tool names, not models).

## Precedence (gateway session)

```
session /model override  (incl. skill-applied switches — last writer wins)
  > slack.channel_models
    > runtime-provider-supplied model
      > model.default
        > provider catalog fallback
```

## Caching / invariants

- A model switch always builds a fresh agent (eviction or config-signature
  change) — the prompt cache restarts once, identical to `/model` today. No
  past context is ever mutated (AGENTS.md caching policy).
- RBAC deny-until-assigned semantics untouched.

## Known edge cases

- A model-only channel entry under a runtime that force-supplies its own model
  (ACP/codex) may mismatch; the fail-open guard falls back to global on any
  resolution error.
- A channel with both a `channel_models` entry and an auto-skill override: the
  skill wins (it writes the session override, which outranks the channel).

## Tests

`tests/agent/test_model_override.py`, `tests/gateway/test_channel_models.py`,
`tests/gateway/test_skill_model_override.py`, `tests/gateway/test_config.py`
(bridging), `tests/tools/test_skill_view_model_override.py`,
`tests/cron/test_cron_skill_model_override.py`,
`tests/tools/test_delegate_model_override.py`.
