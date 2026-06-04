# Always-on per-scope memory summary (holographic) — design

**Date:** 2026-06-03
**Status:** Approved (design); pending implementation plan
**Area:** `plugins/memory/holographic/`, `agent/background_review.py`

## Problem

After moving per-user/per-channel memory into the scoped holographic provider,
those facts are only available when the model actively calls `fact_store`
(search/probe). Unlike the old always-on `USER.md`, nothing is injected into the
system prompt by default, so the model no longer "knows" a user or channel
without querying. We want an always-on, per-scope summary injected into the
system prompt so the model has a baseline understanding of the current user
(in a DM) or channel — refreshed occasionally for **both** user and channel
scopes.

## Goal

Inject a short, LLM-generated **prose summary** of the current scope's memory
into the system prompt by default:

- **DM** → a summary of that user's facts.
- **Channel** → a summary of that channel's shared facts only (never a user's
  private DM profile — privacy-preserving, consistent with the scope isolation
  already built).

Refresh occasionally for whatever scope the session is in (so channels refresh
on the same mechanism as DMs). Keep the fully-offline/free default intact by
making the feature opt-in.

## Approach (chosen)

**Reuse the existing background-review fork** (`agent/background_review.py`),
which already runs every N turns using the agent's own model and credentials
(the user's OpenAI key). Extend it to also refresh the current scope's prose
summary. Generation (write) and injection (read) are decoupled through a cached
row in each scope's SQLite database, so the model never blocks on generation.

Rejected alternatives: a provider-owned LLM client (B — duplicates a background
task pattern and adds credential wiring inside the provider) and a dedicated
periodic summary scheduler (C — most infrastructure for least benefit, YAGNI).

## Data flow

```
[background-review fork, every N turns]          [session start — system prompt build]
  set scope contextvars from agent identity         holographic.system_prompt_block()
  if scope facts changed since last summary:           read cached scope_summary for current scope
    summarize scope's top facts via fork's model       inject "What I know about this {user|channel}"
    store scope_summary(text, signature) in scope DB    cold start → fall back to top-N facts
```

## Components

### 1. Storage — `scope_summary` row per scope DB (`plugins/memory/holographic/store.py`)

A single-row table in each scope's SQLite database:

```sql
CREATE TABLE IF NOT EXISTS scope_summary (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  summary         TEXT NOT NULL,
  fact_signature  TEXT NOT NULL,   -- "<count>:<max(updated_at)>"
  generated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

New `MemoryStore` methods:

- `fact_signature() -> str` — cheap change-detector: `"<COUNT(*) of facts>:<MAX(updated_at)>"`.
  Empty store → e.g. `"0:"`.
- `get_summary() -> dict | None` — returns `{summary, fact_signature, generated_at}` or `None`.
- `set_summary(text: str, signature: str) -> None` — upsert the single row (id=1).

The table is created in `_init_db()` alongside the existing schema (idempotent
`CREATE TABLE IF NOT EXISTS`), so existing scope DBs migrate transparently.

### 2. Injection — `system_prompt_block()` (`plugins/memory/holographic/__init__.py`)

Replace the metadata-only block with:

1. Resolve the current scope bundle (`_bundle_for_current_scope()`).
2. `get_summary()`:
   - If present and non-empty → inject (capped at `summary_max_chars`):
     ```
     ## What I know about this {user|channel}
     <prose summary>
     ```
     Scope label: `user` → "this user"; `chat` → "this channel"; `default` → "this context".
   - Else (cold start, no summary yet) → fall back to the top-N facts by trust
     (existing `list_facts`/retriever), rendered as a short bulleted block, so it
     is useful immediately before the first summary is generated.
   - If the store is empty → return the existing "empty store" guidance line.

The system prompt is built once per session and cached
(`agent/conversation_loop.py`), so this is a **frozen per-session snapshot** —
no mid-conversation mutation, no prefix-cache thrash. Mirrors the old USER.md
snapshot semantics.

When `profile_summary` is disabled, `system_prompt_block()` keeps its current
behavior (active/empty metadata line) — no summary read or fallback rendering.

### 3. Generation — scope-aware review fork (`agent/background_review.py`)

- **Scope fix (required):** the review fork runs in a daemon thread that does
  NOT carry the gateway's session contextvars, but DOES inherit the parent
  agent's identity attributes. At the start of the fork worker, call
  `set_session_vars(...)` from the agent's `_user_id`/`_chat_id`/`_chat_type`
  (+ platform), restoring in a `finally`. This makes holographic's
  `_resolve_scope()` resolve the correct scope inside the fork thread.
  (This also hardens the previously-identified scope-loss gap for any holographic
  operation that runs in the fork.)
- **Refresh step:** when a holographic provider is active AND `profile_summary`
  is enabled AND the scope has facts AND `fact_signature() != cached signature`:
  1. Pull the scope's top `summary_facts` facts (by trust, then recency) AND
     the current cached summary (`get_summary()`).
  2. Build a summarization prompt that includes BOTH the previous summary and the
     latest facts: "Here is the previous summary and the most recent facts about
     this {user|channel}. Produce an updated summary in <= N chars of prose:
     preserve still-relevant knowledge from the previous summary (it may capture
     things no longer in the recent facts), integrate the new facts, and drop
     anything obsolete or contradicted. Factual, no preamble." This makes the
     summary **accretive** — critical knowledge distilled in earlier summaries
     survives even after the underlying facts age out of the top `summary_facts`,
     while the char budget forces ongoing compression so it cannot grow unbounded.
  3. Make **one** non-tool completion call via the fork's existing model client.
  4. `set_summary(result, current_signature)`.

  **Boundary:** to keep the provider free of any LLM-client/credential wiring
  (preserving its offline-by-default nature), the holographic provider exposes a
  helper like `refresh_scope_summary(complete_fn, *, max_chars, max_facts)` where
  `complete_fn(prompt: str) -> str` is supplied by the **fork** (wrapping the
  agent's model). The provider owns fact selection, prompt assembly, signature
  check, and storage; the fork owns the actual model call. So the LLM dependency
  lives in `background_review.py`, not in the provider.
  - Skips cleanly when the signature is unchanged (no model call) or on model
    error (keeps the previous summary; logs at debug). Never raises into the
    fork's main flow.
- Cadence reuses the existing review interval (every `memory_nudge_interval`
  turns) — no new scheduler. Scope-agnostic: refreshes whatever scope the
  session is in, so channels and DMs use the same path.

### 4. Config (opt-in) — `plugins.hermes-memory-store`

- `profile_summary: false` — default OFF (preserves fully-offline/free default).
  ON → uses the agent's model (OpenAI) via the fork.
- `summary_max_chars: 600` — injected-summary budget.
- `summary_facts: 30` — max facts fed to the summarizer.

Advertised via `get_config_schema()`.

## Privacy & cache-safety

- Channel scope summarizes **channel facts only**. The fork reads only the
  resolved scope's store; a user's DM profile can never enter a channel summary.
- Regeneration writes to disk only and affects the **next** session's prompt,
  never the current cached one. Same snapshot semantics as the old USER.md.
- Generation is decoupled from injection — the model never blocks on an LLM
  summary call during a turn.

## Edge cases

- **CLI/cron (default scope):** summary works under the `default` scope; label
  "this context".
- **Empty scope:** no facts → no summary; injection returns the empty-store line.
- **Stale-but-present summary:** inject the cached summary now; the fork refreshes
  it for next session.
- **Model/auth error in fork:** keep prior summary, debug-log, continue.
- **Fork without identity (no gateway context):** resolves to `default` scope;
  must NOT write a user/channel summary into a wrong scope (covered by the
  scope-fix + test).

## Testing

- **Store:** `fact_signature()` changes when a fact is added; `set_summary`/
  `get_summary` round-trip; single-row upsert (no duplicates).
- **Injection:** cached summary rendered with scope-correct header (user vs
  channel); cold-start fallback to top-N facts when no summary; char cap honored;
  `profile_summary: false` → legacy metadata behavior (no summary, no fallback).
- **Generation gating:** unchanged signature → no model call; changed signature →
  regenerates and stores (model call mocked); model error → previous summary kept.
- **Accretion:** when a prior summary exists, the assembled prompt passed to
  `complete_fn` includes the previous summary text (verify the captured prompt
  contains it), so distilled prior knowledge is carried into regeneration.
- **Scope-awareness:** fork with channel identity writes the summary to the
  channel DB, not a user DB; fork with user identity writes to the user DB; fork
  without identity → default scope (no cross-scope write).
- Tests use the `scripts/run_tests.sh` wrapper and must not write real
  `~/.hermes/` (autouse `HERMES_HOME` redirect / `tmp_path`).

## Non-goals (YAGNI)

- No separate summary scheduler/interval (reuse the review cadence).
- No per-scope model override (`summary_model`) — use the agent's model.
- No cross-scope blending (channel + speaker profile).
- No regeneration on every turn; no synchronous generation during a turn.

## Key files

- `plugins/memory/holographic/store.py` — `scope_summary` table + signature/get/set.
- `plugins/memory/holographic/__init__.py` — `system_prompt_block` injection +
  summary-refresh helper (prompt build + model call) + config keys.
- `agent/background_review.py` — scope contextvar set-up in the fork + invoke the
  holographic summary refresh.
