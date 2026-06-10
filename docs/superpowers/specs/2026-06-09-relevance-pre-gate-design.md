# Relevance pre-gate for quiet channels — Design

**Date:** 2026-06-09
**Status:** Approved (design); pending implementation plan
**Scope:** Fork-specific (`everafterai/ea-hermes`), Slack quiet channels

## Problem

In a free-response **quiet channel** (e.g. an issue-tracking channel) the gateway
runs the full agent on **every** message, then leaves it to the agent to decide
whether to engage. That decision is unreliable and expensive:

- The agent replies to messages aimed at humans / not relevant to it.
- It burns API calls (and produces incoherent or spurious output) reasoning about
  whether to act on chatter.
- A reasoning model that *intends* to stay silent produces only reasoning →
  classified `incomplete` → continuation loop (already mitigated, but the root
  waste — running the full agent at all on irrelevant messages — remains).

The fix is a dedicated, cheap **relevance pre-gate**: before the full agent runs,
one fast classifier call decides `act` vs `ignore`. `ignore` ends the turn
silently with no agent run. This separates the *cheap, focused* "should I engage?"
decision from the *expensive* "do the work" agent.

## Decisions (locked during brainstorming)

- **Auto-detect** relevance semantically (no requirement to @mention).
- **Lean silent** when the classifier is uncertain (prompt instructs IGNORE on
  doubt).
- **Coupled to `quiet_channels`** — the gate is the default behavior of quiet
  channels, not a separate channel list. It only "bites" on free-response quiet
  channels; for a mention-only quiet channel it's a no-op (mentions bypass).
- **Core async gateway logic**, not a plugin. `pre_gateway_dispatch` is invoked
  synchronously (`hermes_cli/plugins.py` `invoke_hook`), so a plugin classifier
  would block the event loop and couldn't `await` thread context. Core async lets
  us offload the classifier via `asyncio.to_thread` and `await` thread context.
  This matches how `quiet_channels`/`free_response`/`_is_quiet_channel` are
  implemented (core dispatch behavior, not tools).
- **Fail-open** on classifier error/timeout → run the full agent (never silently
  drop a real issue due to an infra hiccup; a human can always @mention).
- **Purpose** = a per-channel `relevance_gate_purpose` map, falling back to the
  channel's `channel_prompts` entry, then a generic default.

## Non-goals (v1)

- Other platforms (Slack only).
- Caching/deduplicating classifier calls across messages.
- A learned/threshold classifier — it's a single act/ignore LLM call.
- Changing the agent's behavior once it *does* run (that's the existing prompt).

## Architecture

A core async gate in the gateway dispatch path. For a quiet-channel turn that is
**not** an explicit @mention or DM, run one cheap classifier LLM call; `ignore`
ends the turn before the agent runs.

```
inbound message (already passed platform mention/free-response gate)
  → pre_gateway_dispatch hook (existing)
  → RELEVANCE GATE (new, quiet channels only):
       directly addressed (@mention / DM)?  → bypass → act
       else: classify(act|ignore) via cheap model
            ignore → end turn silently (no agent run)
            act / error → proceed to agent (fail-open)
  → full agent (existing)
```

## Components

All helpers pure/injectable for unit testing; the gate orchestration is one async
method on the gateway.

1. **`_relevance_gate_purpose(source, cfg) -> str | None`** — returns the relevance
   purpose for a channel, or `None` if the channel is not a quiet channel (gate
   inactive). Resolution: `slack.relevance_gate_purpose[chat_id]` → that channel's
   `slack.channel_prompts[chat_id]` → a generic default ("Decide whether the
   assistant must take an action relevant to this channel; otherwise ignore.").
   Reuses `_is_quiet_channel` + `_parse_channel_id_list` for the quiet check.

2. **Direct-address signal** — the gate must bypass on explicit @mention or DM.
   The Slack adapter already computes `is_mentioned` (`slack.py:2304`) and
   `is_dm`. Surface it to the gateway via a new `MessageEvent` field
   (`directly_addressed: bool`, default `False`), set in the Slack adapter when
   building the event. DM (`source.chat_type == "dm"`) also counts as directly
   addressed. (Default `False` → other platforms/paths are unaffected.)

3. **`_classify_relevance(purpose, message_text, thread_context, model) -> bool`**
   — returns `True` for "act". Builds a minimal prompt and calls
   `call_llm(model=model, messages=[...], temperature=0, max_tokens=4)` (from
   `agent/auxiliary_client.py`). Parses the reply: starts-with/contains "act" →
   True, else False. The `call_llm` invocation is the single injection point for
   tests (mock it). Prompt shape:
   > System: "You are a relevance filter for a Slack channel. Channel purpose:
   > {purpose}. Decide if the assistant must ACT on the latest message (e.g.
   > track/update/resolve something the channel is for) or IGNORE it (chatter,
   > questions directed at people, general discussion). When unsure, answer
   > IGNORE. Reply with exactly one word: act or ignore."
   > User: "Recent thread context:\n{thread_context}\n\nLatest message:\n{message_text}"

4. **`relevance_gate_model`** — `slack.relevance_gate_model` (optional). A cheap,
   fast model (recommend a nano/mini-class model) used only for the classifier.
   If unset, fall back to the main turn model (works out of the box; user should
   set a cheap one). Passed to `call_llm` as the explicit `model`.

5. **Gate orchestration — `async _relevance_gate_should_skip(event) -> bool`**
   (gateway method, called in the async dispatch right after the
   `pre_gateway_dispatch` hook):
   - `purpose = _relevance_gate_purpose(event.source, cfg)`; if `None` → return
     `False` (not a quiet channel — gate inactive).
   - If `event.directly_addressed` → return `False` (bypass; agent runs).
   - `thread_context = await adapter._fetch_thread_context(...)` when the source
     has a thread (best-effort; empty string on failure — already cached, Tier-3
     safe).
   - `act = await asyncio.to_thread(_classify_relevance, purpose, event.text,
     thread_context, model)`.
   - On any exception/timeout → log and return `False` (**fail-open**).
   - Return `not act` (skip when classifier says ignore).
   - When skipping, log at INFO (channel, reason) — mirrors the
     `pre_gateway_dispatch skip` log — and return before the agent runs (no reply,
     no cost beyond the classifier call).

## Data flow / placement

The gate is invoked in the gateway's message dispatch in `gateway/run.py`, right
after the `pre_gateway_dispatch` hook block (~line 7551–7590) and before the
message is routed to the agent. When `_relevance_gate_should_skip` returns
`True`, dispatch returns early exactly like the `pre_gateway_dispatch` "skip"
path (`return None`).

## Configuration

```yaml
slack:
  quiet_channels: 'C03B4BC9D2P'          # existing — the gate is on for these
  relevance_gate_model: 'gpt-5-nano'      # optional cheap classifier model
  relevance_gate_purpose:                 # optional per-channel; map like channel_prompts
    C03B4BC9D2P: "A bug/issue is reported, updated, or marked resolved (e.g. done / ✅)."
  channel_prompts:
    C03B4BC9D2P: "<agent behavior prompt — also the purpose fallback>"
```

- Absent `relevance_gate_purpose[chat_id]` → fall back to `channel_prompts[chat_id]`
  → generic default.
- `relevance_gate_model` bridged like other slack keys; read via
  `_load_gateway_config()`.

## Error handling

- **Classifier error/timeout** → fail-open (run the agent), logged.
- **No thread context available** → classify on the message alone (empty context).
- **Not a quiet channel** → gate is a complete no-op (returns `False` immediately,
  no LLM call).
- **Directly addressed** → no LLM call, agent runs.

## Testing (fork convention: pure unit tests, mock the model)

- `_relevance_gate_purpose`: explicit map → channel_prompt fallback → default;
  `None` for non-quiet channel.
- `_classify_relevance`: mock `call_llm` returning "act"/"ignore"/"ACT."/garbage →
  correct bool; parsing is lenient.
- `_relevance_gate_should_skip`: directly_addressed → no skip + no model call;
  non-quiet → no skip + no model call; classifier "ignore" → skip; "act" → no
  skip; classifier raises → no skip (fail-open). `call_llm` and
  `_fetch_thread_context` injected/mocked.
- Config bridging of `relevance_gate_model` / `relevance_gate_purpose`.
- Slack adapter sets `event.directly_addressed` for @mention and DM (mirrors the
  existing `source.message_id` adapter test).

## Key invariants preserved

- **Back-compat:** absent quiet-channel config → gate never runs (returns early).
  Other platforms unaffected (`directly_addressed` defaults False; gate only
  active for Slack quiet channels).
- **No prompt-cache impact:** the gate is a pre-dispatch decision; it doesn't alter
  the agent's system prompt or toolsets.
- **RBAC/auth unchanged:** the gate runs after the existing auth path; it only
  decides whether to run the agent, never grants access.
- **Cost bound:** one cheap classifier call per non-mention message in a quiet
  channel; @mention/DM bypass entirely; non-quiet channels never call it.

## Open questions / deferred

- Per-message classifier cost on high-traffic quiet channels — acceptable for v1
  (issue channels are low/moderate traffic); caching deferred.
- Reusing the classifier's "act" decision to pre-seed the agent (e.g. pass the
  detected category) — deferred; keep the gate purely act/ignore.
