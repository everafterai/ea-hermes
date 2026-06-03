# Slack quiet channels + agent-driven reactions — Design

**Date:** 2026-06-03
**Status:** Approved (design); pending implementation plan
**Scope:** Fork-specific (`everafterai/ea-hermes`), Slack-only for v1

## Problem

We want a Slack bot that acts as a low-noise "hidden assistant" in specific
channels. In those channels it should:

1. **Hide all tool calls** — no streamed tool-progress breadcrumbs.
2. **Be able to react with emojis** — including arbitrary custom emoji
   (e.g. `:party_sloth:`) chosen per-situation.
3. **Prefer ending with just an emoji reaction and no text** ~99% of the time,
   **but retain the full ability to reply in text** when asked or when something
   needs explaining.

This is *soft, prompt-driven* emoji-first behavior — **not** hard,
gateway-enforced silence. The bot is never made unable to respond; it is
*encouraged* (via its channel prompt) to react instead of reply, and *given the
capability* (a reaction tool) to do so.

## Non-goals (v1)

- Other platforms (Telegram, Discord, Feishu). Slack only.
- Per-channel `tool_progress` control *without* the full quiet-channel flag
  (a separate generic per-channel display resolver can be added later).
- Configurable/custom lifecycle emoji (the auto 👀→✅/❌ stays fixed).
- Hard reply suppression / a forced "react-only, never reply" mode.

## Background — relevant existing seams

- **Tool-progress streaming** is independent of the agent's `quiet_mode`. It is a
  gateway feature resolved per-platform by `resolve_display_setting(...,
  "tool_progress")` in `gateway/display_config.py`, consumed at
  `gateway/run.py` (~line 15937). Slack's *built-in* default is already `"off"`,
  but a **global** `display.tool_progress: all` overrides it (resolution order:
  per-platform → global → platform-default → global-default).
- **Reaction lifecycle** already exists in the Slack adapter:
  `on_processing_start` adds 👀, `on_processing_complete` swaps it for ✅/❌
  (`gateway/platforms/slack.py:1326-1352`). It is gated to `(is_dm or
  is_mentioned) and _reactions_enabled()` (`slack.py:2234`) and uses
  `adapter._add_reaction()` / `_remove_reaction()` (`slack.py:1291-1319`).
- **Empty/None final response** from the message handler results in **silence**
  at the adapter layer (`gateway/platforms/base.py:3572-3574`) and still records
  a `SUCCESS` outcome (`base.py:3800-3805`). The "⚠️ Processing completed but no
  response was generated" text is injected *earlier*, in
  `gateway/run.py` by `_normalize_empty_agent_response` (~line 1558, applied
  ~line 8867). So the warning — not the silence — is what we must gate.
- **Approvals & clarifications** are sent **mid-run as independent messages**
  (`send_exec_approval` ~`run.py:17004`, `send_clarify` ~`run.py:16888`), not via
  the final response. They are therefore unaffected by any final-response
  handling and always reach the channel.
- **Tools reach the live adapter** via `_gateway_runner_ref()` →
  `runner.adapters.get(Platform.SLACK)` (pattern in
  `tools/send_message_tool.py:490-505`).
- **Session context** exposes the triggering message + channel to tools:
  `HERMES_SESSION_MESSAGE_ID`, `HERMES_SESSION_CHAT_ID`,
  `HERMES_SESSION_PLATFORM` (`gateway/session_context.py:51-63`).
- **Toolsets** are declared in `toolsets.py` (`TOOLSETS` dict); a
  platform-scoped toolset follows the `discord` pattern (`toolsets.py:271`), and
  `_TOOLSET_PLATFORM_RESTRICTIONS` in `hermes_cli/tools_config.py:132` limits a
  toolset to specific platforms.
- **RBAC** gates toolsets per role (`gateway/tool_access.py`) with a
  `pre_tool_call` execution backstop in `model_tools.py`. Fail-closed.

## Architecture — three composable components

### Component 1 — `slack_react` tool (new)

An agent-callable tool that lets the LLM add/remove a Slack emoji reaction.

- **Signature:**
  `slack_react(emoji: str, message_id: str | None = None, remove: bool = False) -> str`
  - `emoji`: Slack short name, no colons (`party_sloth`, `white_check_mark`).
  - `message_id`: target message ts; defaults to the triggering message
    (`HERMES_SESSION_MESSAGE_ID`).
  - `remove`: remove the reaction instead of adding (lets a workflow swap an
    interim emoji for a final one).
- **Execution:** resolve channel from `HERMES_SESSION_CHAT_ID`; resolve the live
  Slack adapter via `_gateway_runner_ref()`; call the existing
  `adapter._add_reaction()` / `_remove_reaction()`. Returns a concise success or
  error string (e.g. when there is no live adapter — CLI/cron — or the active
  platform is not Slack).
- **Toolset / RBAC:** register a new platform-restricted `slack` toolset in
  `toolsets.py` with `"tools": ["slack_react"]`, restricted to Slack via
  `_TOOLSET_PLATFORM_RESTRICTIONS`. Add `slack_react` to the `hermes-slack`
  bundle. Roles that should be allowed to react are granted the `slack` toolset;
  the `pre_tool_call` RBAC backstop enforces it. Fail-closed: a role without the
  toolset cannot react.
- **Scope:** generally useful in *any* Slack channel; not coupled to quiet
  channels.

### Component 2 — quiet channels (config + minimal gateway change)

- **Config:** `slack.quiet_channels` — comma-separated channel IDs, mirroring
  `free_response_channels`. Bridged into the platform runtime `extra` in
  `gateway/config.py` next to the existing `slack:` keys. Hand-edited under
  `slack:` directly (per fork convention), not `slack.extra`.
- **Hard behaviors for a quiet channel (output-only; triggering is unchanged —
  @mention, or every message when also in `free_response_channels`):**
  1. **Force `tool_progress: off`.** When resolving tool-progress for a turn whose
     channel is in `quiet_channels`, treat it as `off` regardless of global
     `display.tool_progress`. (Implemented at the `run.py` tool-progress
     resolution site, which already has `source.chat_id` available.)
  2. **Permit silent (emoji-only) completion.** When the agent finishes a turn in
     a quiet channel with **no text** and **no error**, do **not** apply the
     `_normalize_empty_agent_response` warning — let the response stay `None` so
     the adapter is silent and records `SUCCESS` (👀→✅ as usual).
- **Always-preserved behaviors (NOT changed):**
  - **Any text the agent produces is posted normally.** No suppression of real
    replies — ever.
  - **Errors surface.** An empty-but-errored turn is still surfaced (warning/❌),
    exactly as today; only the *successful* empty turn is allowed to be silent.
  - **Approvals & clarifications** continue to post mid-run.
  - **Reaction lifecycle** (auto 👀→✅/❌) is unchanged; `slack_react` adds emoji
    on top ("auto-only + tool extra").

### Component 3 — `channel_prompts` (existing, no code)

The emoji-first *behavior* is steered entirely by the per-channel prompt, e.g.:

```yaml
slack:
  free_response_channels: 'C03B4BC9D2P'
  quiet_channels: 'C03B4BC9D2P'
  channel_prompts:
    C03B4BC9D2P: |
      You are a quiet assistant in this channel. Do the work without narrating.
      Prefer to finish by calling slack_react with a single fitting emoji and
      producing NO text reply:
        - fully done  -> slack_react "party_sloth"
        - acknowledged/working stored -> slack_react "eyes"
      Only reply in text when explicitly asked, or when you must explain a
      decision, surface a problem, or ask a question.
```

## End-to-end flow

1. User @mentions the bot (or posts in a `free_response` quiet channel).
2. Gateway auto-adds 👀; `tool_progress` is forced off (no breadcrumbs).
3. Agent does its work with tools (hidden).
4. Agent calls `slack_react("party_sloth")` per its channel prompt.
5. Agent produces no final text → quiet channel suppresses the empty-response
   warning → silent. Adapter records SUCCESS → 👀 swaps to ✅.
6. Result on the user's message: 🦥 + ✅, no text, no tool lines.
7. If the user instead says "explain what you changed," the agent simply replies
   in text — no mode switch, no command.

## Configuration summary

```yaml
slack:
  require_mention: true
  free_response_channels: 'C03B4BC9D2P'
  quiet_channels: 'C03B4BC9D2P'        # NEW: hide tool calls + allow emoji-only completion
  channel_prompts:
    C03B4BC9D2P: "<emoji-first instructions, see above>"
```

Plus the `slack` toolset granted to the relevant role(s) in `slack.roles` /
`slack.user_roles` (RBAC).

## Error handling

- No live adapter (CLI/cron) or non-Slack active platform → `slack_react`
  returns a clear error string; the agent can continue (it is not fatal).
- Invalid emoji / missing scope / already-reacted → adapter `_add_reaction`
  already logs at debug and returns False; the tool surfaces a concise message.
- Agent-level errors in quiet channels → surfaced as today (the silent-completion
  gate applies only to *successful* empty turns).

## Testing strategy (fork convention: prefer pure unit tests)

- `slack_react`: targets `HERMES_SESSION_MESSAGE_ID` by default; honors explicit
  `message_id`; `remove=True` path; mocked adapter via `_gateway_runner_ref`;
  graceful error when no live adapter / wrong platform.
- Config bridging: `slack.quiet_channels` parsed (comma-split) into platform
  `extra`; empty/absent → feature off (back-compat).
- Quiet-channel tool-progress: forced `off` for a listed channel even when global
  `display.tool_progress: all`; unaffected for non-listed channels.
- Silent completion: empty + success in a quiet channel → `None` (no warning);
  empty + error → warning preserved; non-empty → text posted unchanged; all of
  the above in a non-quiet channel behave exactly as today.
- RBAC: `slack_react` denied for a role lacking the `slack` toolset (policy +
  `pre_tool_call` backstop).

## Key invariants preserved

- **Prompt caching:** no mid-conversation toolset swaps or system-prompt rebuilds
  introduced; `slack_react` lives in a normally-resolved toolset.
- **RBAC fail-closed:** absent config → upstream behavior; the new tool is
  deny-until-granted.
- **Multi-user isolation:** `slack_react` derives channel/message strictly from
  per-task session contextvars (no global state), consistent with existing tools.
- **Back-compat:** absent `quiet_channels` → no behavior change anywhere.

## Implementation notes (deviations from this design, as built)

Two intentional refinements were made during implementation; the code is the
source of truth:

- **`slack_react` posts directly to the Slack Web API** (fresh `aiohttp` session
  to `reactions.add`/`reactions.remove`, mirroring
  `tools/send_message_tool.py:_send_slack`) rather than reaching the live adapter
  via `_gateway_runner_ref()` + `adapter._add_reaction()`. This is loop-safe from
  the tool's worker thread and avoids event-loop/adapter coupling. Functionally
  equivalent for a single-workspace bot token.
- **`quiet_channels` is read from the raw top-level `slack:` config block**
  (via `_load_gateway_config()` in `_is_quiet_channel`) rather than being bridged
  into the platform runtime `extra`. The consumer lives in `gateway/run.py`, so
  no bridge is needed; this is a deliberate departure from how
  `free_response_channels` is wired (that key is consumed in the adapter).

## Open questions / deferred

- Whether to also expose a generic per-channel `tool_progress` override (without
  the full quiet flag) — deferred; not required for this use case.
- Cross-platform generalization (base-adapter reaction tool) — deferred to a
  future iteration once the Slack version is proven.
- The `slack` toolset defaults ON; without RBAC active, `slack_react` is
  available to all authorized Slack users (consistent with other default-on
  toolsets). Enable RBAC (`slack.user_roles`) to restrict it per role.
