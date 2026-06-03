# Per-scope holographic memory — design

**Date:** 2026-06-03
**Status:** Approved (design); pending implementation plan
**Area:** `plugins/memory/holographic/` (fork-local memory provider)

## Problem

The gateway runs one shared `HERMES_HOME` for many messaging-platform users. The
**built-in file memory store** (`USER.md` / `MEMORY.md`) is **not scoped per user**:
`get_memory_dir()` returns `get_hermes_home() / "memories"` with no identity
component, so every Slack user reads and writes the **same** `USER.md` profile.
Personal preferences therefore leak across users.

The bundled `holographic` provider (`plugins/memory/holographic/`) is fully local
(SQLite, no API keys, no paid service) and is the cheapest path to real per-user
memory — but it currently **ignores `user_id`**: `initialize(session_id, **kwargs)`
receives identity in `kwargs` and drops it, storing all facts in one shared
`memory_store.db`.

## Goal

Give the holographic provider **per-scope isolation** so that:

- **DMs are per-user silos** — Shai and Shachar have completely separate memory in
  their DMs.
- **Channels share knowledge** — anyone talking to the bot in `#production-issues`
  reads/writes the same channel memory (threads in the channel inherit it).

Stay **free and fully offline** (no mem0, no external embeddings, no new services),
and keep the change **backward-compatible** with upstream (off by default).

## Scope rule

```
chat_type == "dm"                       -> ("user", user_id)
chat_type in {"channel","group","thread"} -> ("chat", chat_id)
no identity (CLI / cron / unset)        -> ("default", "default")
```

`chat_type` values come from `gateway/session.py:83`
(`"dm" | "group" | "channel" | "thread"`).

## Feasibility (verified)

- Identity is forwarded into the provider: `agent/agent_init.py:1104-1143` passes
  `user_id`, `chat_id`, **and** `chat_type` into `MemoryManager.initialize_all`,
  which fans them out to each provider's `initialize()`
  (`agent/memory_manager.py:592-609`).
- The same identity is readable **at tool-call time** from session contextvars
  (`gateway/session_context.py:51-85`) via
  `get_session_env("HERMES_SESSION_CHAT_TYPE" | "_USER_ID" | "_CHAT_ID")` — the
  exact mechanism the RBAC backstop uses (`gateway/tool_access.py:235-244`).
- **Critical constraint:** the `MemoryProvider` instance is a **process-global
  singleton** shared across all concurrent gateway sessions; `initialize()` is
  called fresh per message but `self._store` is shared. Therefore the scope MUST be
  resolved **per operation from contextvars**, never cached from init kwargs.

## Approach: per-scope SQLite file (physical isolation)

Chosen over a single-DB `scope`-column approach because physical file separation
makes cross-scope leakage structurally impossible (no forgotten `WHERE` clause can
bleed data), requires **zero changes to `store.py`'s SQL**, and matches the fork's
existing DM-vs-channel session partitioning ethos. Trade-off accepted: many small
DB files and no cross-scope entity graph (that is the intended behavior).

### Components

**A. Scope resolver** (pure function, unit-testable)

```python
def _resolve_scope() -> tuple[str, str]:
    from gateway.session_context import get_session_env
    chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "")
    user_id   = get_session_env("HERMES_SESSION_USER_ID", "")
    chat_id   = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if chat_type == "dm" and user_id:
        return ("user", user_id)
    if chat_id:
        return ("chat", chat_id)
    if user_id:
        return ("user", user_id)        # DM-ish fallback when chat_type missing
    return ("default", "default")        # CLI / cron / no identity
```

Resolution order of `get_session_env` is contextvar → `os.environ` → default, so
CLI/cron fall through to `("default", "default")`.

**B. Per-scope store cache** (concurrency-safe; replaces the single `self._store`)

```python
self._scopes: dict[tuple[str, str], _ScopeBundle] = {}   # bundle = (store, retriever)
self._scopes_lock = threading.RLock()

def _bundle_for_current_scope(self) -> _ScopeBundle:
    scope = _resolve_scope()
    with self._scopes_lock:
        b = self._scopes.get(scope)
        if b is None:
            db_path = self._db_path_for_scope(scope)
            store = MemoryStore(db_path=db_path,
                                default_trust=self._default_trust,
                                hrr_dim=self._hrr_dim)
            retriever = FactRetriever(store=store, ...)
            b = _ScopeBundle(store=store, retriever=retriever)
            self._scopes[scope] = b
        return b
```

`MemoryStore` already uses `check_same_thread=False` plus its own `RLock`, so
concurrent sessions touching different scopes are safe; the cache dict gets its own
lock. Optional LRU eviction cap is deferred (YAGNI) — unbounded is fine for a team.

**C. File layout & naming**

```
~/.hermes/memories/holographic/
  user_<sanitized-user-id>.db
  chat_<sanitized-chat-id>.db
  default.db
```

- New config key `db_dir` (default `$HERMES_HOME/memories/holographic/`,
  `$HERMES_HOME` expanded like the existing `db_path` handling).
- Sanitize: `re.sub(r"[^A-Za-z0-9_-]", "_", scope_id)`; if sanitization changed the
  string or it exceeds a length cap, append a short deterministic hash of the
  original to avoid collisions.
- **Path-containment guard:** resolve the final path and assert it stays within
  `db_dir` so a crafted `chat_id` (e.g. `../../etc/x`) cannot escape the base dir.

**D. Operation wiring** — every provider method that currently uses `self._store` /
`self._retriever` instead calls `_bundle_for_current_scope()`:
`_handle_fact_store`, `_handle_fact_feedback`, `prefetch`, `system_prompt_block`,
`on_session_end` (auto-extract). `system_prompt_block` and `prefetch` degrade
gracefully to the `default` scope if contextvars are unset at build time.

### Activation flag (backward-compatible)

New config `scope_isolation` under `plugins.hermes-memory-store`:

- **absent / `false`** → current single shared `db_path` behavior (upstream-exact,
  clean merges, single-user CLI unaffected).
- **`true`** → per-scope `db_dir` mode described above.

This mirrors the fork's established "feature off = upstream behavior exactly"
activation pattern (RBAC activates on `user_roles` presence). The VM config sets
`scope_isolation: true`.

### Built-in store changes (closing the leak) — configuration only, no code

- `memory.user_profile_enabled: false` — stops the leaky shared `USER.md` from
  loading, injecting, or being written (`agent/agent_init.py:1078-1083`).
- `memory.memory_enabled: true` — keep `MEMORY.md` as **global** env/VM/project
  notes (genuinely shared, not a privacy leak on a single-purpose VM).
- `memory.provider: holographic` — enable the scoped provider.
- In `scope_isolation: true` mode, `HolographicMemoryProvider.on_memory_write`
  becomes a **no-op**: otherwise global `MEMORY.md` writes would be double-stored
  into a per-scope silo. Holographic facts come only from explicit `fact_store`
  tool calls (plus optional `auto_extract` at session end).
- Existing `USER.md` content is **abandoned, not migrated** (it is cross-user soup);
  scopes start empty.

## Testing

- **Isolation (headline guarantee):** write a fact under `user:A`; assert `user:B`
  and `chat:X` searches never return it. Model on the existing multi-user
  `session_search` isolation tests under `tests/`.
- **Scope resolution:** unit table — `dm`→user, `channel`/`group`/`thread`→chat,
  no-identity→default, `dm` with missing `chat_type` → user fallback.
- **Sanitization / path-traversal:** malicious `chat_id` (`../../etc`) stays
  contained within `db_dir`; collision handling for sanitized duplicates.
- **Concurrency:** interleaved scopes across threads via contextvars; assert no
  cross-scope bleed and no `sqlite3` threading errors.
- **Backward-compat:** `scope_isolation: false` reproduces today's single-file
  behavior exactly.
- **Singleton safety:** two `initialize()` calls with different identities do not
  cross state; operations route by contextvar scope, not init kwargs.
- Tests must not write to real `~/.hermes/` (autouse `HERMES_HOME` redirect);
  exercise via the `scripts/run_tests.sh` wrapper.

## Non-goals (YAGNI)

- No `scope` column / SQL rewrites in `store.py` or `retrieval.py`.
- No cross-scope "shared knowledge" tier inside holographic (global env facts live
  in the built-in `MEMORY.md`).
- No mem0 / external embeddings / external services.
- No migration of existing `USER.md` or `memory_store.db` data.
- No LRU cache eviction until a real scope-count problem is observed.

## Key files

- `plugins/memory/holographic/__init__.py` — provider; gets scope cache + wiring +
  config keys (`db_dir`, `scope_isolation`) + no-op `on_memory_write` when scoped.
- `plugins/memory/holographic/store.py` — **unchanged** (each scope is a separate
  `db_path`).
- `agent/agent_init.py:1104-1143`, `agent/memory_manager.py:592-609` — identity
  plumbing (already forwards `user_id`/`chat_id`/`chat_type`; no change needed).
- `gateway/session_context.py:51-85` — contextvars read at operation time.
- `~/.hermes/config.yaml` (VM) — `memory.provider`, `user_profile_enabled: false`,
  `memory_enabled: true`, `plugins.hermes-memory-store.scope_isolation: true`.
