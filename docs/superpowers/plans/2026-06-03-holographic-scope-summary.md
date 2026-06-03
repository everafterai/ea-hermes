# Always-on Per-Scope Memory Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject an always-on, LLM-generated prose summary of the current scope's memory (DM user or channel) into the system prompt, refreshed by the existing background-review fork and cached per-scope in SQLite.

**Architecture:** Generation (write) and injection (read) are decoupled through a `scope_summary` row in each scope's SQLite DB. Holographic's `system_prompt_block()` reads the cached summary; the scope-aware background-review fork regenerates it via a one-shot `call_llm` when the scope's facts change. The provider stays LLM-client-free by taking an injected `complete_fn`. Opt-in (`profile_summary`); channel scope summarizes channel facts only; summaries are accretive (prior summary fed back in).

**Tech Stack:** Python, SQLite (`plugins/memory/holographic/store.py`), `MemoryProvider`, `agent/background_review.py`, `agent.auxiliary_client.call_llm`, `gateway/session_context` contextvars, pytest via `scripts/run_tests.sh`.

**Reference spec:** `docs/superpowers/specs/2026-06-03-holographic-scope-summary-design.md`

---

## Orientation (read before starting)

- Files changed: `plugins/memory/holographic/store.py` (storage), `plugins/memory/holographic/__init__.py` (config + injection + refresh helper), `agent/background_review.py` (scope-aware refresh trigger). Plus tests and README.
- Existing helpers you will reuse:
  - `_resolve_scope() -> (kind, id)` and `_bundle_for_current_scope() -> (store, retriever)` in `plugins/memory/holographic/__init__.py` (already scope-aware).
  - `MemoryStore.list_facts(category=None, min_trust=0.0, limit=50)` returns facts ordered by `trust_score` desc (`store.py`).
  - `agent.auxiliary_client.call_llm(task, messages, max_tokens, temperature, timeout, main_runtime)` — one-shot completion; returns an object where `response.choices[0].message.content` is the text (see `agent/title_generator.py` for the exact usage pattern).
  - `agent._current_main_runtime()` — the parent runtime dict (used in `background_review.py:372`).
  - `gateway/session_context.set_session_vars(...)` / `clear_session_vars(tokens)` — set/restore identity contextvars (thread-local).
  - `MemoryManager.get_provider(name)` (`agent/memory_manager.py:309`).
- Run tests ONLY via the wrapper; pytest selectors/flags go AFTER `--`:
  `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestX`
  Activate the venv first if needed: `source .venv/bin/activate`.
- Cache-safety: `system_prompt_block()` is read once per session at prompt-build time and cached; never mutate it per turn. Regeneration only writes to disk (affects the next session).

---

## Task 1: `scope_summary` storage in MemoryStore

**Files:**
- Modify: `plugins/memory/holographic/store.py` (append table to `_SCHEMA`; add 3 methods)
- Test: `tests/agent/test_holographic_summary.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_holographic_summary.py`:

```python
"""Tests for per-scope summary storage + injection + refresh (holographic)."""

from plugins.memory.holographic.store import MemoryStore


class TestScopeSummaryStorage:
    def _store(self, tmp_path):
        return MemoryStore(db_path=str(tmp_path / "scope.db"))

    def test_signature_empty_then_changes_with_facts(self, tmp_path):
        s = self._store(tmp_path)
        sig0 = s.fact_signature()
        assert sig0.startswith("0:")
        s.add_fact("user prefers dark mode", category="user_pref")
        sig1 = s.fact_signature()
        assert sig1 != sig0
        assert sig1.startswith("1:")

    def test_get_summary_none_when_unset(self, tmp_path):
        s = self._store(tmp_path)
        assert s.get_summary() is None

    def test_set_then_get_summary_roundtrip_and_upsert(self, tmp_path):
        s = self._store(tmp_path)
        s.set_summary("They like concise answers.", "1:2026-06-03 00:00:00")
        got = s.get_summary()
        assert got["summary"] == "They like concise answers."
        assert got["fact_signature"] == "1:2026-06-03 00:00:00"
        # Upsert (single row): a second set replaces, not duplicates.
        s.set_summary("Updated.", "2:2026-06-03 01:00:00")
        got2 = s.get_summary()
        assert got2["summary"] == "Updated."
        assert got2["fact_signature"] == "2:2026-06-03 01:00:00"
        count = s._conn.execute("SELECT COUNT(*) FROM scope_summary").fetchone()[0]
        assert count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestScopeSummaryStorage`
Expected: FAIL — `AttributeError: 'MemoryStore' object has no attribute 'fact_signature'`.

- [ ] **Step 3: Append the table to `_SCHEMA`**

In `plugins/memory/holographic/store.py`, inside the `_SCHEMA = """ ... """` string, add this table definition just before the closing `"""` (after the `memory_banks` table):

```sql

CREATE TABLE IF NOT EXISTS scope_summary (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    summary         TEXT NOT NULL,
    fact_signature  TEXT NOT NULL,
    generated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

(`_init_db()` already runs `self._conn.executescript(_SCHEMA)`, so this is created idempotently for new and existing scope DBs.)

- [ ] **Step 4: Add the three methods**

In `plugins/memory/holographic/store.py`, add these methods to the `MemoryStore` class (e.g. right after `record_feedback`):

```python
    def fact_signature(self) -> str:
        """Cheap change-detector for the facts table: '<count>:<max updated_at>'."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m FROM facts"
            ).fetchone()
            return f"{row['c']}:{row['m']}"

    def get_summary(self) -> "dict | None":
        """Return the cached scope summary dict, or None if unset."""
        with self._lock:
            row = self._conn.execute(
                "SELECT summary, fact_signature, generated_at FROM scope_summary WHERE id = 1"
            ).fetchone()
            return dict(row) if row else None

    def set_summary(self, summary: str, fact_signature: str) -> None:
        """Upsert the single-row scope summary."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scope_summary (id, summary, fact_signature, generated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    summary = excluded.summary,
                    fact_signature = excluded.fact_signature,
                    generated_at = excluded.generated_at
                """,
                (summary, fact_signature),
            )
            self._conn.commit()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestScopeSummaryStorage`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add plugins/memory/holographic/store.py tests/agent/test_holographic_summary.py
git commit -m "feat(memory): per-scope summary storage (scope_summary table + signature)"
```

---

## Task 2: Config keys for the summary feature

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (`__init__`, `initialize`, `get_config_schema`)
- Test: `tests/agent/test_holographic_summary.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_holographic_summary.py`:

```python
from plugins.memory.holographic import HolographicMemoryProvider


class TestSummaryConfig:
    def test_defaults_off(self, tmp_path):
        p = HolographicMemoryProvider(config={"db_dir": str(tmp_path / "h")})
        p.initialize(session_id="t")
        assert p._profile_summary is False
        assert p._summary_max_chars == 600
        assert p._summary_facts == 30

    def test_enabled_and_overrides(self, tmp_path):
        p = HolographicMemoryProvider(config={
            "db_dir": str(tmp_path / "h"),
            "profile_summary": True,
            "summary_max_chars": 400,
            "summary_facts": 10,
        })
        p.initialize(session_id="t")
        assert p._profile_summary is True
        assert p._summary_max_chars == 400
        assert p._summary_facts == 10

    def test_schema_advertises_summary_keys(self, tmp_path):
        p = HolographicMemoryProvider(config={})
        keys = {e["key"] for e in p.get_config_schema()}
        assert "profile_summary" in keys
        assert "summary_max_chars" in keys
        assert "summary_facts" in keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestSummaryConfig`
Expected: FAIL — `AttributeError: ... '_profile_summary'`.

- [ ] **Step 3: Add config fields in `__init__` and `initialize`**

In `plugins/memory/holographic/__init__.py`, in `HolographicMemoryProvider.__init__`, add these defaults alongside the existing init fields (e.g. after `self._temporal_decay = 0`):

```python
        self._profile_summary = False
        self._summary_max_chars = 600
        self._summary_facts = 30
```

In `initialize`, add these reads alongside the existing `self._hrr_dim = ...` etc.:

```python
        self._profile_summary = str(self._config.get("profile_summary", "false")).strip().lower() in (
            "1", "true", "yes", "on",
        )
        self._summary_max_chars = int(self._config.get("summary_max_chars", 600))
        self._summary_facts = int(self._config.get("summary_facts", 30))
```

- [ ] **Step 4: Advertise the keys in `get_config_schema`**

In `get_config_schema`, add these entries to the returned list (after the existing entries, before the return closes):

```python
            {"key": "profile_summary", "description": "Inject an always-on LLM summary of the current scope's memory into the system prompt (uses your model)", "default": "false", "choices": ["true", "false"]},
            {"key": "summary_max_chars", "description": "Max characters of the injected scope summary", "default": "600"},
            {"key": "summary_facts", "description": "Max facts fed to the summary generator", "default": "30"},
```

- [ ] **Step 5: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestSummaryConfig`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_summary.py
git commit -m "feat(memory): config keys for holographic scope summary (opt-in)"
```

---

## Task 3: `refresh_scope_summary()` — accretive regeneration

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (add `_scope_label`, `_build_summary_prompt`, `refresh_scope_summary`)
- Test: `tests/agent/test_holographic_summary.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_holographic_summary.py`:

```python
from gateway.session_context import set_session_vars, clear_session_vars


class TestRefreshScopeSummary:
    def _provider(self, tmp_path, **cfg):
        base = {"db_dir": str(tmp_path / "h"), "profile_summary": True}
        base.update(cfg)
        p = HolographicMemoryProvider(config=base)
        p.initialize(session_id="t")
        return p

    def test_disabled_is_noop(self, tmp_path):
        p = self._provider(tmp_path, profile_summary=False)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "likes tea"})
            calls = []
            assert p.refresh_scope_summary(lambda prompt: calls.append(prompt) or "x") is False
            assert calls == []  # model never called
        finally:
            clear_session_vars(tokens)

    def test_generates_when_facts_present(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "User is an analytics PM"})
            captured = {}
            def fake(prompt):
                captured["prompt"] = prompt
                return "Analytics PM; prefers concise replies."
            assert p.refresh_scope_summary(fake) is True
            store, _ = p._bundle_for_current_scope()
            assert store.get_summary()["summary"] == "Analytics PM; prefers concise replies."
            # prompt mentions the scope label and includes the fact
            assert "this user" in captured["prompt"]
            assert "analytics PM" in captured["prompt"].lower()
        finally:
            clear_session_vars(tokens)

    def test_skips_when_signature_unchanged(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact one"})
            assert p.refresh_scope_summary(lambda prompt: "S1") is True
            n = {"calls": 0}
            def fake(prompt):
                n["calls"] += 1
                return "S2"
            assert p.refresh_scope_summary(fake) is False  # unchanged signature
            assert n["calls"] == 0
        finally:
            clear_session_vars(tokens)

    def test_accretion_includes_prior_summary(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact A"})
            p.refresh_scope_summary(lambda prompt: "PRIOR-SUMMARY-TEXT")
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact B"})
            captured = {}
            p.refresh_scope_summary(lambda prompt: captured.setdefault("p", prompt) or "new")
            assert "PRIOR-SUMMARY-TEXT" in captured["p"]
        finally:
            clear_session_vars(tokens)

    def test_channel_label_in_prompt(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="channel", user_id="U1", chat_id="C1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "deploy on fridays"})
            captured = {}
            p.refresh_scope_summary(lambda prompt: captured.setdefault("p", prompt) or "s")
            assert "this channel" in captured["p"]
        finally:
            clear_session_vars(tokens)

    def test_model_error_keeps_prior_summary(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact A"})
            p.refresh_scope_summary(lambda prompt: "GOOD")
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact B"})
            def boom(prompt):
                raise RuntimeError("model down")
            assert p.refresh_scope_summary(boom) is False
            store, _ = p._bundle_for_current_scope()
            assert store.get_summary()["summary"] == "GOOD"  # unchanged
        finally:
            clear_session_vars(tokens)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestRefreshScopeSummary`
Expected: FAIL — `AttributeError: ... 'refresh_scope_summary'`.

- [ ] **Step 3: Implement the helpers**

In `plugins/memory/holographic/__init__.py`, add these methods to `HolographicMemoryProvider` (e.g. after `_bundle_for_current_scope`):

```python
    def _scope_label(self) -> str:
        kind, _ = _resolve_scope()
        return {"user": "this user", "chat": "this channel"}.get(kind, "this context")

    def _build_summary_prompt(self, prior: str, facts: list) -> str:
        label = self._scope_label()
        fact_lines = "\n".join(f"- {f.get('content', '')}" for f in facts)
        prior_block = (
            f"PREVIOUS SUMMARY (preserve still-relevant knowledge from this):\n{prior}\n\n"
            if prior else ""
        )
        return (
            f"You maintain a running profile of {label} for an assistant.\n\n"
            f"{prior_block}"
            f"MOST RECENT FACTS:\n{fact_lines}\n\n"
            f"Write an updated summary of {label} in <= {self._summary_max_chars} characters of plain prose. "
            f"Preserve still-relevant knowledge from the previous summary (it may capture things no longer in the "
            f"recent facts), integrate the new facts, and drop anything obsolete or contradicted. "
            f"Be factual and specific. Return ONLY the summary text — no preamble, no headers, no quotes."
        )

    def refresh_scope_summary(self, complete_fn) -> bool:
        """Regenerate the current scope's cached summary if its facts changed.

        ``complete_fn(prompt: str) -> str`` performs a single model completion;
        it is supplied by the caller (the background-review fork) so the provider
        carries no LLM client. Returns True if a new summary was stored.
        """
        if not self._profile_summary:
            return False
        try:
            store, _ = self._bundle_for_current_scope()
            sig = store.fact_signature()
            if sig.startswith("0:"):
                return False  # no facts yet
            cached = store.get_summary()
            if cached and cached.get("fact_signature") == sig:
                return False  # unchanged since last summary
            facts = store.list_facts(limit=self._summary_facts)
            if not facts:
                return False
            prior = (cached or {}).get("summary", "") or ""
            text = (complete_fn(self._build_summary_prompt(prior, facts)) or "").strip()
            if not text:
                return False
            if len(text) > self._summary_max_chars:
                text = text[: self._summary_max_chars].rstrip()
            store.set_summary(text, sig)
            return True
        except Exception as e:
            logger.debug("Holographic scope summary refresh failed: %s", e)
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestRefreshScopeSummary`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_summary.py
git commit -m "feat(memory): accretive per-scope summary regeneration via injected complete_fn"
```

---

## Task 4: Inject the summary in `system_prompt_block()`

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (`system_prompt_block`)
- Test: `tests/agent/test_holographic_summary.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_holographic_summary.py`:

```python
class TestSystemPromptInjection:
    def _provider(self, tmp_path, **cfg):
        base = {"db_dir": str(tmp_path / "h"), "profile_summary": True}
        base.update(cfg)
        p = HolographicMemoryProvider(config=base)
        p.initialize(session_id="t")
        return p

    def test_injects_cached_summary_with_user_label(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact A"})
            p.refresh_scope_summary(lambda prompt: "They are an analytics PM who likes brevity.")
            block = p.system_prompt_block()
            assert "What I know about this user" in block
            assert "analytics PM" in block
        finally:
            clear_session_vars(tokens)

    def test_channel_label(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="channel", user_id="U1", chat_id="C1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact A"})
            p.refresh_scope_summary(lambda prompt: "Channel for prod incidents.")
            assert "What I know about this channel" in p.system_prompt_block()
        finally:
            clear_session_vars(tokens)

    def test_cold_start_falls_back_to_facts(self, tmp_path):
        p = self._provider(tmp_path)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "uses pytest heavily"})
            block = p.system_prompt_block()  # no summary generated yet
            assert "What I know about this user" in block
            assert "uses pytest heavily" in block
        finally:
            clear_session_vars(tokens)

    def test_char_cap_enforced(self, tmp_path):
        p = self._provider(tmp_path, summary_max_chars=20)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact A"})
            p.refresh_scope_summary(lambda prompt: "x" * 200)
            block = p.system_prompt_block()
            assert "x" * 21 not in block  # summary body capped at 20
        finally:
            clear_session_vars(tokens)

    def test_disabled_keeps_legacy_metadata(self, tmp_path):
        p = self._provider(tmp_path, profile_summary=False)
        tokens = set_session_vars(chat_type="dm", user_id="U1", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "fact A"})
            block = p.system_prompt_block()
            assert "What I know about" not in block
            assert "Holographic Memory" in block  # legacy active line
        finally:
            clear_session_vars(tokens)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestSystemPromptInjection`
Expected: FAIL — current `system_prompt_block` emits only the legacy metadata line, so the "What I know about" assertions fail.

- [ ] **Step 3: Rewrite `system_prompt_block`**

In `plugins/memory/holographic/__init__.py`, replace the existing `system_prompt_block` method with:

```python
    def system_prompt_block(self) -> str:
        try:
            store, _ = self._bundle_for_current_scope()
        except Exception:
            return ""
        try:
            total = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except Exception:
            total = 0

        if self._profile_summary:
            label = self._scope_label()
            try:
                summ = store.get_summary()
            except Exception:
                summ = None
            if summ and summ.get("summary"):
                body = summ["summary"][: self._summary_max_chars]
                return f"## What I know about {label}\n{body}"
            # Cold start: no summary yet — fall back to top facts so it is
            # useful immediately before the first summary is generated.
            if total:
                try:
                    facts = store.list_facts(limit=min(5, self._summary_facts))
                except Exception:
                    facts = []
                if facts:
                    lines = "\n".join(f"- {f.get('content', '')}" for f in facts)
                    return f"## What I know about {label}\n{lines}"

        # Legacy metadata behavior (summary disabled, or empty store).
        if total == 0:
            return (
                "# Holographic Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, preferences, decisions.\n"
                "Use fact_feedback to rate facts after using them (trains trust scores)."
            )
        return (
            f"# Holographic Memory\n"
            f"Active. {total} facts stored with entity resolution and trust scoring.\n"
            f"Use fact_store to search, probe entities, reason across entities, or add facts.\n"
            f"Use fact_feedback to rate facts after using them (trains trust scores)."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestSystemPromptInjection`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_summary.py
git commit -m "feat(memory): inject per-scope summary into system prompt (cold-start fallback to facts)"
```

---

## Task 5: Scope-aware refresh trigger in the background-review fork

**Files:**
- Modify: `agent/background_review.py` (add `_refresh_holographic_scope_summary`; call it in the worker)
- Test: `tests/agent/test_holographic_summary.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_holographic_summary.py`:

```python
from unittest.mock import patch


class _FakeProvider:
    """Captures the scope visible (via contextvars) when refresh is invoked."""
    def __init__(self):
        self.invoked = False
        self.seen_complete_result = None

    def refresh_scope_summary(self, complete_fn):
        from plugins.memory.holographic import _resolve_scope
        self.invoked = True
        self.scope = _resolve_scope()
        # Exercise the supplied complete_fn so we cover the call_llm wrapper.
        self.seen_complete_result = complete_fn("PROMPT")
        return True


class _FakeManager:
    def __init__(self, provider):
        self._p = provider
    def get_provider(self, name):
        return self._p if name == "holographic" else None


class _FakeAgent:
    def __init__(self, manager, **identity):
        self._memory_manager = manager
        self.platform = identity.get("platform", "slack")
        self._user_id = identity.get("user_id")
        self._chat_id = identity.get("chat_id")
        self._chat_type = identity.get("chat_type")
    def _current_main_runtime(self):
        return {"model": "gpt-x", "api_key": "k"}


class _FakeLLMResp:
    class _Choice:
        class _Msg:
            content = "SUMMARY TEXT"
        message = _Msg()
    choices = [_Choice()]


class TestBackgroundRefreshTrigger:
    def test_refresh_runs_with_channel_scope(self):
        from agent.background_review import _refresh_holographic_scope_summary
        prov = _FakeProvider()
        agent = _FakeAgent(_FakeManager(prov), user_id="U1", chat_id="C1", chat_type="channel")
        with patch("agent.auxiliary_client.call_llm", return_value=_FakeLLMResp()) as m:
            _refresh_holographic_scope_summary(agent)
        assert prov.invoked is True
        assert prov.scope == ("chat", "C1")     # channel scope resolved in the worker
        assert prov.seen_complete_result == "SUMMARY TEXT"
        assert m.called

    def test_refresh_runs_with_user_scope(self):
        from agent.background_review import _refresh_holographic_scope_summary
        prov = _FakeProvider()
        agent = _FakeAgent(_FakeManager(prov), user_id="U1", chat_id="D1", chat_type="dm")
        with patch("agent.auxiliary_client.call_llm", return_value=_FakeLLMResp()):
            _refresh_holographic_scope_summary(agent)
        assert prov.scope == ("user", "U1")

    def test_no_provider_is_safe(self):
        from agent.background_review import _refresh_holographic_scope_summary
        agent = _FakeAgent(_FakeManager(None), user_id="U1", chat_id="D1", chat_type="dm")
        _refresh_holographic_scope_summary(agent)  # must not raise

    def test_contextvars_restored_after(self):
        from agent.background_review import _refresh_holographic_scope_summary
        from gateway.session_context import get_session_env
        prov = _FakeProvider()
        agent = _FakeAgent(_FakeManager(prov), user_id="U1", chat_id="C1", chat_type="channel")
        with patch("agent.auxiliary_client.call_llm", return_value=_FakeLLMResp()):
            _refresh_holographic_scope_summary(agent)
        # After the call, the worker thread's vars are cleared (empty), not "C1".
        assert get_session_env("HERMES_SESSION_CHAT_ID", "") in ("", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestBackgroundRefreshTrigger`
Expected: FAIL — `ImportError: cannot import name '_refresh_holographic_scope_summary'`.

- [ ] **Step 3: Add the refresh function and call it in the worker**

In `agent/background_review.py`, add this module-level function (near the other module-level helpers, after the imports):

```python
def _refresh_holographic_scope_summary(agent) -> None:
    """Refresh the current scope's holographic summary, scoped to the agent's
    identity. Runs inside the background-review daemon thread, which does NOT
    carry the gateway session contextvars — so we set them from the agent's
    inherited identity, run the refresh, then restore. No-op if the holographic
    provider is absent or the feature is off (the provider self-gates)."""
    mm = getattr(agent, "_memory_manager", None)
    if mm is None:
        return
    try:
        provider = mm.get_provider("holographic")
    except Exception:
        provider = None
    if provider is None or not hasattr(provider, "refresh_scope_summary"):
        return

    from gateway.session_context import set_session_vars, clear_session_vars

    platform = getattr(agent, "platform", "") or ""
    platform = getattr(platform, "value", platform)  # enum -> str if needed
    tokens = set_session_vars(
        platform=str(platform) if platform else "",
        chat_id=getattr(agent, "_chat_id", "") or "",
        chat_type=getattr(agent, "_chat_type", "") or "",
        user_id=getattr(agent, "_user_id", "") or "",
    )
    try:
        def _complete(prompt: str) -> str:
            from agent.auxiliary_client import call_llm
            resp = call_llm(
                task="scope_summary",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.3,
                main_runtime=agent._current_main_runtime(),
            )
            return (resp.choices[0].message.content or "")
        provider.refresh_scope_summary(_complete)
    except Exception as e:
        logger.debug("Scope summary refresh skipped: %s", e)
    finally:
        clear_session_vars(tokens)
```

Then call it once at the start of the worker. In `agent/background_review.py`, inside the worker function `_run_background_review_worker` (the daemon-thread entry, defined around line 330), right after the `_set_approval_callback(_bg_review_auto_deny)` try/except block and before `review_agent = None`, add:

```python
    # Refresh the per-scope holographic summary (scoped to the agent's identity).
    # Independent of the memory/skill review below; self-gates when disabled.
    try:
        _refresh_holographic_scope_summary(agent)
    except Exception:
        logger.debug("background scope-summary refresh raised", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_summary.py -- -k TestBackgroundRefreshTrigger`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/background_review.py tests/agent/test_holographic_summary.py
git commit -m "feat(memory): refresh per-scope holographic summary in scope-aware review fork"
```

---

## Task 6: Full regression + docs

**Files:**
- Modify: `plugins/memory/holographic/README.md`
- Test: regression across memory + background-review suites

- [ ] **Step 1: Run the full relevant suites**

Run:
```
scripts/run_tests.sh tests/agent/test_holographic_summary.py tests/agent/test_holographic_scope.py tests/agent/test_memory_provider.py tests/agent/test_memory_user_id.py tests/run_agent/test_background_review_summary.py
```
Expected: all PASS. If `test_background_review_summary.py` or another suite breaks because the worker now calls `_refresh_holographic_scope_summary`, confirm the cause: the function self-guards on `_memory_manager`/provider being absent, so a fork without a holographic provider must be a clean no-op. If a test fork lacks the attributes, the `getattr(..., None)` guards should handle it — only adjust the production guards if a real gap is found (do NOT weaken the scope-restoration `finally`).

- [ ] **Step 2: Document in the plugin README**

Edit `plugins/memory/holographic/README.md` — add this under the per-scope isolation section:

```markdown
### Always-on per-scope summary

Set `profile_summary: true` to inject an always-on prose summary of the current
scope's memory into the system prompt (DM → that user; channel → that channel's
shared facts only). The summary is generated by your model via the existing
background-review fork (every N turns, only when the scope's facts changed) and
cached per scope in SQLite. It is accretive — the previous summary is fed back
in so distilled knowledge survives as individual facts age out. Defaults off
(keeps the provider fully offline). Tune with `summary_max_chars` (default 600)
and `summary_facts` (default 30). Until the first summary exists, the top facts
are injected as a fallback.
```

- [ ] **Step 3: Commit**

```bash
git add plugins/memory/holographic/README.md
git commit -m "docs(memory): document always-on per-scope holographic summary"
```

---

## Operator runbook (VM config — not committed)

To enable on the gateway, add under `plugins.hermes-memory-store` in `~/.hermes/config.yaml`:

```yaml
plugins:
  hermes-memory-store:
    scope_isolation: true
    db_dir: $HERMES_HOME/memories/holographic
    profile_summary: true       # turn on the always-on summary
    summary_max_chars: 600
    summary_facts: 30
```

Restart the gateway. Summaries populate after the background review runs (every
~10 turns in a scope, only once that scope's facts change). New DM/channel scopes
show the top-facts fallback until their first summary is generated.

---

## Self-Review

- **Spec coverage:** storage (Task 1), config opt-in (Task 2), accretive regeneration incl. prior-summary feedback (Task 3), injection + cold-start fallback + scope labels + disabled behavior (Task 4), scope-aware fork trigger via `call_llm`/`complete_fn` (Task 5), privacy (channel label/scope tested in Tasks 3–4), regression + docs + runbook (Task 6). All spec sections map to a task.
- **Placeholder scan:** none — every code step shows complete code and exact commands.
- **Type consistency:** `refresh_scope_summary(complete_fn) -> bool`, `_build_summary_prompt(prior, facts)`, `_scope_label() -> str`, `fact_signature()/get_summary()/set_summary()` are used consistently across Tasks 1, 3, 4, 5. The fork supplies `complete_fn(prompt) -> str` wrapping `call_llm(...).choices[0].message.content`, matching the provider's call site.
- **Known limitation (documented):** summary refresh rides the background-review cadence; if the background review never runs (e.g. review disabled), summaries won't refresh — acceptable per the design's "reuse the review interval" decision.
