# Per-scope Holographic Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope the holographic memory provider so DMs are per-user silos and channels share knowledge, fully offline, backward-compatible with upstream.

**Architecture:** The provider stays a process-global singleton. Instead of one `self._store`, it keeps a thread-safe `{(kind, id): (store, retriever)}` cache and resolves the current scope **per operation** from session contextvars (`dm → user:<user_id>`, else `chat:<chat_id>`). Each scope is a separate SQLite file under `db_dir`. A `scope_isolation` config flag gates the new behavior (off = today's single shared file). No SQL changes to `store.py`.

**Tech Stack:** Python, SQLite (`plugins/memory/holographic/store.py`), `MemoryProvider` ABC, `gateway/session_context` contextvars, pytest via `scripts/run_tests.sh`.

**Reference spec:** `docs/superpowers/specs/2026-06-03-holographic-per-scope-memory-design.md`

---

## Orientation (read before starting)

- File to change: `plugins/memory/holographic/__init__.py` (the `HolographicMemoryProvider` class + new module-level helpers). `store.py` and `retrieval.py` are **unchanged**.
- Constructors you will reuse verbatim:
  - `MemoryStore(db_path=..., default_trust=..., hrr_dim=...)` (`plugins/memory/holographic/store.py:101`)
  - `FactRetriever(store=..., temporal_decay_half_life=..., hrr_weight=..., hrr_dim=...)` (`plugins/memory/holographic/retrieval.py:25`)
- Identity at runtime: `get_session_env("HERMES_SESSION_CHAT_TYPE"|"_USER_ID"|"_CHAT_ID", "")` from `gateway/session_context.py:161`. `chat_type` ∈ `{"dm","group","channel","thread"}` (`gateway/session.py:83`).
- Tests set identity with `set_session_vars(...)` and must restore with `clear_session_vars(tokens)` in a `finally` (`gateway/session_context.py:103,136`).
- Run tests ONLY through the wrapper: `scripts/run_tests.sh tests/agent/test_holographic_scope.py` (CI parity). Add `-v` for verbose.
- The provider is a process-global singleton; `initialize()` runs per message. **Never** clear the `_scopes` cache inside `initialize()` or `shutdown()` — other concurrent scopes may be live.

---

## Task 1: Scope resolver helper

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (add module-level `_resolve_scope`)
- Test: `tests/agent/test_holographic_scope.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_holographic_scope.py`:

```python
"""Per-scope isolation tests for the holographic memory provider."""

import json
import pytest

from gateway.session_context import set_session_vars, clear_session_vars
from plugins.memory.holographic import (
    HolographicMemoryProvider,
    _resolve_scope,
    _sanitize_scope_id,
)


def _with_scope(**kwargs):
    """Context manager-ish helper: set session vars, yield, then clear."""
    return set_session_vars(**kwargs)


class TestResolveScope:
    def test_dm_resolves_to_user(self):
        tokens = set_session_vars(chat_type="dm", user_id="U_SHAI", chat_id="D123")
        try:
            assert _resolve_scope() == ("user", "U_SHAI")
        finally:
            clear_session_vars(tokens)

    def test_channel_resolves_to_chat(self):
        tokens = set_session_vars(chat_type="channel", user_id="U_SHAI", chat_id="C_PROD")
        try:
            assert _resolve_scope() == ("chat", "C_PROD")
        finally:
            clear_session_vars(tokens)

    def test_group_and_thread_resolve_to_chat(self):
        for ctype in ("group", "thread"):
            tokens = set_session_vars(chat_type=ctype, user_id="U1", chat_id="C9")
            try:
                assert _resolve_scope() == ("chat", "C9")
            finally:
                clear_session_vars(tokens)

    def test_dm_without_chat_type_falls_back_to_user(self):
        tokens = set_session_vars(chat_type="", user_id="U_ONLY", chat_id="")
        try:
            assert _resolve_scope() == ("user", "U_ONLY")
        finally:
            clear_session_vars(tokens)

    def test_no_identity_resolves_to_default(self):
        tokens = set_session_vars(chat_type="", user_id="", chat_id="")
        try:
            assert _resolve_scope() == ("default", "default")
        finally:
            clear_session_vars(tokens)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestResolveScope -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_scope'`.

- [ ] **Step 3: Add the helper to `plugins/memory/holographic/__init__.py`**

Add these imports near the top (after the existing `import re`):

```python
import hashlib
import threading
from pathlib import Path
```

Add these module-level helpers after the `logger = logging.getLogger(__name__)` line:

```python
def _resolve_scope() -> "tuple[str, str]":
    """Resolve the current memory scope from session contextvars.

    DMs are per-user silos; channels/groups/threads share by chat_id.
    Falls back to ("default", "default") for CLI/cron or missing identity.
    """
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return ("default", "default")
    chat_type = (get_session_env("HERMES_SESSION_CHAT_TYPE", "") or "").strip()
    user_id = (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip()
    chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    if chat_type == "dm" and user_id:
        return ("user", user_id)
    if chat_id:
        return ("chat", chat_id)
    if user_id:
        return ("user", user_id)
    return ("default", "default")


_SCOPE_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_scope_id(scope_id: str) -> str:
    """Make a scope id safe for use in a filename.

    Replaces every non-[A-Za-z0-9_-] char with '_'. If anything was replaced
    or the id is long, appends a short deterministic hash of the original to
    avoid post-sanitization collisions.
    """
    safe = _SCOPE_SAFE_RE.sub("_", scope_id)
    if safe != scope_id or len(safe) > 64:
        digest = hashlib.sha1(scope_id.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:48]}_{digest}"
    return safe
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestResolveScope -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_scope.py
git commit -m "feat(memory): add holographic scope resolver + id sanitizer"
```

---

## Task 2: Scope → DB path mapping with path-containment guard

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (provider config fields + `_db_path_for_scope`)
- Test: `tests/agent/test_holographic_scope.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_holographic_scope.py`:

```python
class TestDbPathForScope:
    def _provider(self, tmp_path, scope_isolation=True):
        p = HolographicMemoryProvider(config={
            "scope_isolation": scope_isolation,
            "db_dir": str(tmp_path / "holo"),
            "db_path": str(tmp_path / "legacy.db"),
        })
        p.initialize(session_id="t")
        return p

    def test_user_scope_path(self, tmp_path):
        p = self._provider(tmp_path)
        path = p._db_path_for_scope(("user", "U_SHAI"))
        assert path.endswith("/holo/user_U_SHAI.db")

    def test_chat_scope_path(self, tmp_path):
        p = self._provider(tmp_path)
        path = p._db_path_for_scope(("chat", "C_PROD"))
        assert path.endswith("/holo/chat_C_PROD.db")

    def test_malicious_id_cannot_escape_base_dir(self, tmp_path):
        p = self._provider(tmp_path)
        path = p._db_path_for_scope(("chat", "../../etc/passwd"))
        base = (tmp_path / "holo").resolve()
        assert base in Path(path).resolve().parents
        assert "/" not in Path(path).name.replace(".db", "")

    def test_legacy_mode_ignores_scope(self, tmp_path):
        p = self._provider(tmp_path, scope_isolation=False)
        a = p._db_path_for_scope(("user", "A"))
        b = p._db_path_for_scope(("chat", "B"))
        assert a == b == str(tmp_path / "legacy.db")
```

Add `from pathlib import Path` to the test file imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestDbPathForScope -v`
Expected: FAIL — `AttributeError: ... has no attribute '_db_path_for_scope'`.

- [ ] **Step 3: Rewrite `__init__` and `initialize`, add `_db_path_for_scope`**

Replace the `HolographicMemoryProvider.__init__` method with:

```python
    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._min_trust = float(self._config.get("min_trust_threshold", 0.3))
        # Per-scope store cache (provider is a process-global singleton).
        self._scopes: dict[tuple[str, str], tuple] = {}
        self._scopes_lock = threading.RLock()
        # Populated in initialize().
        self._scope_isolation = False
        self._db_dir = Path(".")
        self._legacy_db_path = ""
        self._default_trust = 0.5
        self._hrr_dim = 1024
        self._hrr_weight = 0.3
        self._temporal_decay = 0
        self._session_id = ""
```

Replace the `initialize` method with:

```python
    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        hermes_home = str(get_hermes_home())

        def _expand(value):
            if isinstance(value, str):
                return value.replace("$HERMES_HOME", hermes_home).replace("${HERMES_HOME}", hermes_home)
            return value

        self._scope_isolation = str(self._config.get("scope_isolation", "false")).strip().lower() in (
            "1", "true", "yes", "on",
        )
        self._legacy_db_path = _expand(self._config.get("db_path", hermes_home + "/memory_store.db"))
        db_dir = _expand(self._config.get("db_dir", hermes_home + "/memories/holographic"))
        self._db_dir = Path(db_dir).expanduser()
        if self._scope_isolation:
            self._db_dir.mkdir(parents=True, exist_ok=True)
        self._default_trust = float(self._config.get("default_trust", 0.5))
        self._hrr_dim = int(self._config.get("hrr_dim", 1024))
        self._hrr_weight = float(self._config.get("hrr_weight", 0.3))
        self._temporal_decay = int(self._config.get("temporal_decay_half_life", 0))
        self._session_id = session_id
        # NOTE: do NOT touch self._scopes here. The provider is shared across
        # concurrent gateway sessions; re-initialising on one message must not
        # disturb other live scopes.

    def _db_path_for_scope(self, scope: "tuple[str, str]") -> str:
        if not self._scope_isolation:
            return self._legacy_db_path
        kind, ident = scope
        safe = _sanitize_scope_id(ident)
        base = self._db_dir.resolve()
        target = (base / f"{kind}_{safe}.db").resolve()
        if base not in target.parents:
            raise ValueError(f"scope db path escapes base dir: {target}")
        return str(target)
```

Note: the old `initialize` created `self._store`/`self._retriever`; those attributes are gone. Task 3 wires the rest of the methods to the new cache. The provider will not be fully functional between Task 2 and Task 3 — that is expected; Task 3 finishes the refactor.

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestDbPathForScope -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_scope.py
git commit -m "feat(memory): map holographic scope to per-scope db path with traversal guard"
```

---

## Task 3: Per-scope store cache + wire all provider operations

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (add `_bundle_for_current_scope`; rewire `_handle_fact_store`, `_handle_fact_feedback`, `prefetch`, `system_prompt_block`, `on_session_end`, `_auto_extract_facts`, `shutdown`)
- Test: `tests/agent/test_holographic_scope.py`

- [ ] **Step 1: Write the failing test (the headline isolation guarantee)**

Append to `tests/agent/test_holographic_scope.py`:

```python
class TestScopeIsolation:
    def _provider(self, tmp_path):
        p = HolographicMemoryProvider(config={
            "scope_isolation": True,
            "db_dir": str(tmp_path / "holo"),
        })
        p.initialize(session_id="t")
        return p

    def _add(self, provider, content):
        return provider.handle_tool_call("fact_store", {"action": "add", "content": content})

    def _search(self, provider, query):
        out = provider.handle_tool_call("fact_store", {"action": "search", "query": query})
        return json.loads(out)

    def test_dm_users_are_isolated(self, tmp_path):
        p = self._provider(tmp_path)

        tokens = set_session_vars(chat_type="dm", user_id="U_SHAI", chat_id="D1")
        try:
            self._add(p, "Shai prefers no em dashes")
        finally:
            clear_session_vars(tokens)

        tokens = set_session_vars(chat_type="dm", user_id="U_SHACHAR", chat_id="D2")
        try:
            res = self._search(p, "em dashes")
            assert res["count"] == 0
        finally:
            clear_session_vars(tokens)

        tokens = set_session_vars(chat_type="dm", user_id="U_SHAI", chat_id="D1")
        try:
            res = self._search(p, "em dashes")
            assert res["count"] == 1
        finally:
            clear_session_vars(tokens)

    def test_channel_is_shared_across_users(self, tmp_path):
        p = self._provider(tmp_path)

        tokens = set_session_vars(chat_type="channel", user_id="U_SHAI", chat_id="C_PROD")
        try:
            self._add(p, "prod deploy uses blue green rollout")
        finally:
            clear_session_vars(tokens)

        # Different user, same channel -> must see the fact.
        # NOTE: avoid hyphens in FTS5 test queries — SQLite FTS5 treats '-' as a
        # column-filter/negation operator, so "blue-green" would match nothing.
        tokens = set_session_vars(chat_type="channel", user_id="U_SHACHAR", chat_id="C_PROD")
        try:
            res = self._search(p, "blue green rollout")
            assert res["count"] == 1
        finally:
            clear_session_vars(tokens)

    def test_model_supplied_identity_arg_cannot_cross_scope(self, tmp_path):
        """Hardening: scope is server-derived from contextvars. A model-supplied
        user_id/chat_id arg on the tool call must be ignored, so it cannot reach
        another user's or channel's memory."""
        p = self._provider(tmp_path)

        tokens = set_session_vars(chat_type="dm", user_id="U_SHAI", chat_id="D1")
        try:
            self._add(p, "Shai private banking pin reminder")
        finally:
            clear_session_vars(tokens)

        # Attacker session (different user) tries to inject Shai's identity as args.
        tokens = set_session_vars(chat_type="dm", user_id="U_ATTACKER", chat_id="D9")
        try:
            out = p.handle_tool_call("fact_store", {
                "action": "search",
                "query": "banking pin",
                "user_id": "U_SHAI",      # bogus override attempt
                "chat_id": "D1",          # bogus override attempt
                "scope": "user:U_SHAI",   # bogus override attempt
            })
            assert json.loads(out)["count"] == 0
        finally:
            clear_session_vars(tokens)

    def test_dm_and_channel_for_same_user_are_separate(self, tmp_path):
        p = self._provider(tmp_path)

        tokens = set_session_vars(chat_type="dm", user_id="U_SHAI", chat_id="D1")
        try:
            self._add(p, "secret personal note")
        finally:
            clear_session_vars(tokens)

        tokens = set_session_vars(chat_type="channel", user_id="U_SHAI", chat_id="C_PROD")
        try:
            res = self._search(p, "secret personal note")
            assert res["count"] == 0
        finally:
            clear_session_vars(tokens)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestScopeIsolation -v`
Expected: FAIL — provider operations still reference the removed `self._store`/`self._retriever` (AttributeError).

- [ ] **Step 3: Add the cache accessor and rewire the operations**

Add this method to the class (e.g. right after `_db_path_for_scope`):

```python
    def _bundle_for_current_scope(self):
        """Return the (store, retriever) bundle for the current session scope,
        creating and caching it on first use. Thread-safe."""
        scope = _resolve_scope()
        with self._scopes_lock:
            bundle = self._scopes.get(scope)
            if bundle is None:
                db_path = self._db_path_for_scope(scope)
                store = MemoryStore(
                    db_path=db_path,
                    default_trust=self._default_trust,
                    hrr_dim=self._hrr_dim,
                )
                retriever = FactRetriever(
                    store=store,
                    temporal_decay_half_life=self._temporal_decay,
                    hrr_weight=self._hrr_weight,
                    hrr_dim=self._hrr_dim,
                )
                bundle = (store, retriever)
                self._scopes[scope] = bundle
            return bundle
```

Replace the first lines of `_handle_fact_store` — change:

```python
    def _handle_fact_store(self, args: dict) -> str:
        try:
            action = args["action"]
            store = self._store
            retriever = self._retriever
```

to:

```python
    def _handle_fact_store(self, args: dict) -> str:
        try:
            store, retriever = self._bundle_for_current_scope()
            action = args["action"]
```

Replace the body of `_handle_fact_feedback` — change:

```python
    def _handle_fact_feedback(self, args: dict) -> str:
        try:
            fact_id = int(args["fact_id"])
            helpful = args["action"] == "helpful"
            result = self._store.record_feedback(fact_id, helpful=helpful)
            return json.dumps(result)
```

to:

```python
    def _handle_fact_feedback(self, args: dict) -> str:
        try:
            store, _ = self._bundle_for_current_scope()
            fact_id = int(args["fact_id"])
            helpful = args["action"] == "helpful"
            result = store.record_feedback(fact_id, helpful=helpful)
            return json.dumps(result)
```

Replace `system_prompt_block` with:

```python
    def system_prompt_block(self) -> str:
        try:
            store, _ = self._bundle_for_current_scope()
            total = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except Exception:
            total = 0
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

Replace `prefetch` with:

```python
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query:
            return ""
        try:
            _, retriever = self._bundle_for_current_scope()
            results = retriever.search(query, min_trust=self._min_trust, limit=5)
            if not results:
                return ""
            lines = []
            for r in results:
                trust = r.get("trust_score", r.get("trust", 0))
                lines.append(f"- [{trust:.1f}] {r.get('content', '')}")
            return "## Holographic Memory\n" + "\n".join(lines)
        except Exception as e:
            logger.debug("Holographic prefetch failed: %s", e)
            return ""
```

Replace `on_session_end` with:

```python
    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._config.get("auto_extract", False):
            return
        if not messages:
            return
        store, _ = self._bundle_for_current_scope()
        self._auto_extract_facts(store, messages)
```

Change the signature of `_auto_extract_facts` from `def _auto_extract_facts(self, messages: list) -> None:` to `def _auto_extract_facts(self, store, messages: list) -> None:` and replace its two `self._store.add_fact(...)` calls with `store.add_fact(...)`.

Replace `shutdown` with:

```python
    def shutdown(self) -> None:
        # Provider is a process-global singleton; do NOT close cached per-scope
        # stores here — other concurrent sessions may still be using them.
        # SQLite connections are released at process exit.
        pass
```

`on_memory_write` also still references the removed `self._store`. To satisfy the
"no stale `self._store`" grep gate WITHOUT yet adding Task 4's behavioral change,
rewire only its store access here (Task 4 adds the `scope_isolation` no-op guard):

```python
    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes as facts."""
        if action == "add" and content:
            try:
                store, _ = self._bundle_for_current_scope()
                category = "user_pref" if target == "user" else "general"
                store.add_fact(content, category=category)
            except Exception as e:
                logger.debug("Holographic memory_write mirror failed: %s", e)
```

After this task, confirm zero stale refs:
`grep -n "self\._store\|self\._retriever" plugins/memory/holographic/__init__.py`
should print nothing.

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestScopeIsolation -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_scope.py
git commit -m "feat(memory): scope holographic operations via per-scope store cache"
```

---

## Task 4: `on_memory_write` no-op in scoped mode

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (`on_memory_write`)
- Test: `tests/agent/test_holographic_scope.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestOnMemoryWrite:
    def test_noop_when_scoped(self, tmp_path):
        p = HolographicMemoryProvider(config={
            "scope_isolation": True,
            "db_dir": str(tmp_path / "holo"),
        })
        p.initialize(session_id="t")
        tokens = set_session_vars(chat_type="dm", user_id="U_SHAI", chat_id="D1")
        try:
            p.on_memory_write("add", "user", "global env note")
            out = p.handle_tool_call("fact_store", {"action": "list"})
            assert json.loads(out)["count"] == 0
        finally:
            clear_session_vars(tokens)

    def test_mirrors_when_not_scoped(self, tmp_path):
        p = HolographicMemoryProvider(config={
            "scope_isolation": False,
            "db_path": str(tmp_path / "legacy.db"),
        })
        p.initialize(session_id="t")
        p.on_memory_write("add", "user", "a preference")
        out = p.handle_tool_call("fact_store", {"action": "list"})
        assert json.loads(out)["count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestOnMemoryWrite -v`
Expected: FAIL — `test_noop_when_scoped` finds 1 fact (mirror still active).

- [ ] **Step 3: Update `on_memory_write`**

Replace `on_memory_write` with:

```python
    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes as facts.

        Disabled in scoped mode: global MEMORY.md notes must not be copied into
        a per-scope silo (they live in the global built-in store instead).
        """
        if self._scope_isolation:
            return
        if action == "add" and content:
            try:
                store, _ = self._bundle_for_current_scope()
                category = "user_pref" if target == "user" else "general"
                store.add_fact(content, category=category)
            except Exception as e:
                logger.debug("Holographic memory_write mirror failed: %s", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestOnMemoryWrite -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_scope.py
git commit -m "feat(memory): disable holographic memory mirror in scoped mode"
```

---

## Task 5: Backward-compat + concurrency safety

**Files:**
- Test: `tests/agent/test_holographic_scope.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
import threading


class TestBackwardCompatAndConcurrency:
    def test_legacy_single_file_shared_across_scopes(self, tmp_path):
        p = HolographicMemoryProvider(config={
            "scope_isolation": False,
            "db_path": str(tmp_path / "legacy.db"),
        })
        p.initialize(session_id="t")

        tokens = set_session_vars(chat_type="dm", user_id="A", chat_id="D1")
        try:
            p.handle_tool_call("fact_store", {"action": "add", "content": "shared fact xyz"})
        finally:
            clear_session_vars(tokens)

        # Different scope, but legacy mode = one shared file -> visible.
        tokens = set_session_vars(chat_type="dm", user_id="B", chat_id="D2")
        try:
            out = p.handle_tool_call("fact_store", {"action": "search", "query": "shared fact xyz"})
            assert json.loads(out)["count"] == 1
        finally:
            clear_session_vars(tokens)

    def test_concurrent_scopes_do_not_bleed(self, tmp_path):
        p = HolographicMemoryProvider(config={
            "scope_isolation": True,
            "db_dir": str(tmp_path / "holo"),
        })
        p.initialize(session_id="t")
        errors = []

        def worker(uid):
            # Each thread gets a fresh contextvar context.
            tokens = set_session_vars(chat_type="dm", user_id=uid, chat_id=f"D_{uid}")
            try:
                for i in range(5):
                    p.handle_tool_call("fact_store", {"action": "add", "content": f"{uid} fact {i}"})
                out = p.handle_tool_call("fact_store", {"action": "list", "limit": 100})
                contents = [f["content"] for f in json.loads(out)["facts"]]
                if any(not c.startswith(uid) for c in contents):
                    errors.append((uid, contents))
            finally:
                clear_session_vars(tokens)

        threads = [threading.Thread(target=worker, args=(f"U{n}",)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
```

- [ ] **Step 2: Run test to verify it passes (no code change expected)**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestBackwardCompatAndConcurrency -v`
Expected: PASS (2 tests). These exercise behavior already implemented in Tasks 2–3; if either fails, fix the cache/legacy-path logic before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/agent/test_holographic_scope.py
git commit -m "test(memory): cover holographic legacy mode + concurrent scope isolation"
```

---

## Task 6: Expose new config keys in `get_config_schema`

**Files:**
- Modify: `plugins/memory/holographic/__init__.py` (`get_config_schema`)
- Test: `tests/agent/test_holographic_scope.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestConfigSchema:
    def test_schema_advertises_scope_keys(self):
        p = HolographicMemoryProvider(config={})
        keys = {entry["key"] for entry in p.get_config_schema()}
        assert "scope_isolation" in keys
        assert "db_dir" in keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestConfigSchema -v`
Expected: FAIL — keys missing.

- [ ] **Step 3: Update `get_config_schema`**

Replace `get_config_schema` with:

```python
    def get_config_schema(self):
        from hermes_constants import display_hermes_home
        _home = display_hermes_home()
        _default_db = f"{_home}/memory_store.db"
        _default_dir = f"{_home}/memories/holographic"
        return [
            {"key": "scope_isolation", "description": "Per-DM-user / per-channel memory isolation (Slack multi-user gateway)", "default": "false", "choices": ["true", "false"]},
            {"key": "db_dir", "description": "Directory for per-scope SQLite DBs (used when scope_isolation=true)", "default": _default_dir},
            {"key": "db_path", "description": "Single SQLite DB path (used when scope_isolation=false)", "default": _default_db},
            {"key": "auto_extract", "description": "Auto-extract facts at session end", "default": "false", "choices": ["true", "false"]},
            {"key": "default_trust", "description": "Default trust score for new facts", "default": "0.5"},
            {"key": "hrr_dim", "description": "HRR vector dimensions", "default": "1024"},
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py::TestConfigSchema -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/holographic/__init__.py tests/agent/test_holographic_scope.py
git commit -m "feat(memory): advertise scope_isolation + db_dir in holographic config schema"
```

---

## Task 7: Full-suite check + docs

**Files:**
- Modify: `plugins/memory/holographic/README.md`
- Test: full provider suite (regression)

- [ ] **Step 1: Run the holographic + memory provider suites for regressions**

Run: `scripts/run_tests.sh tests/agent/test_holographic_scope.py tests/agent/test_memory_provider.py tests/agent/test_memory_user_id.py -v`
Expected: all PASS. If `test_memory_provider.py` references the removed `self._store`/`self._retriever` attributes directly, update those tests to call `_bundle_for_current_scope()` (set a `default` scope) or to use `handle_tool_call`; do not re-introduce the removed attributes.

- [ ] **Step 2: Document the new behavior in the plugin README**

Edit `plugins/memory/holographic/README.md` — add a "Per-scope isolation" section documenting:

```markdown
## Per-scope isolation (multi-user gateway)

Set `scope_isolation: true` under `plugins.hermes-memory-store` to give each
DM user and each channel its own fact store:

- DM  -> `db_dir/user_<user_id>.db`  (per-user silo)
- channel/group/thread -> `db_dir/chat_<chat_id>.db`  (shared by participants)
- CLI / cron -> `db_dir/default_default.db`

Default `db_dir` is `$HERMES_HOME/memories/holographic`. When
`scope_isolation` is false (default) the plugin uses the single shared
`db_path`, exactly as before. Scope is resolved per operation from session
contextvars, so a single process safely serves many concurrent users.
```

- [ ] **Step 3: Commit**

```bash
git add plugins/memory/holographic/README.md
git commit -m "docs(memory): document holographic per-scope isolation"
```

---

## Task 8: Operator runbook (VM config — not committed)

This task is applied on the gateway VM's `~/.hermes/config.yaml`, NOT in the repo (it is host config, and may sit near secrets). It is documented here so the deploy step is not lost.

- [ ] **Step 1: Edit the VM `config.yaml`**

Under the top-level `memory:` block:

```yaml
memory:
  provider: holographic
  memory_enabled: true          # keep MEMORY.md as global env/VM notes
  user_profile_enabled: false   # stop the shared USER.md leak
```

Under `plugins:`:

```yaml
plugins:
  hermes-memory-store:
    scope_isolation: true
    db_dir: $HERMES_HOME/memories/holographic
    auto_extract: false
```

- [ ] **Step 2: Restart the gateway and smoke-test**

- From two different users' DMs, store a personal preference each, then have each ask the bot to recall it — confirm no cross-user bleed.
- In a shared channel, have user A teach a fact and user B recall it — confirm it is shared.
- Confirm `~/.hermes/memories/holographic/` contains `user_*.db` and `chat_*.db` files.

- [ ] **Step 3: (Optional) retire the old shared profile**

The pre-existing `~/.hermes/memories/USER.md` is no longer injected. Archive or delete it once the new behavior is confirmed.

---

## Self-Review

- **Spec coverage:** scope rule (Task 1), per-scope file + sanitization + containment (Task 2), singleton-safe per-op cache (Task 3), `on_memory_write` no-op (Task 4), backward-compat + concurrency (Task 5), config keys (Task 6), built-in `USER.md` off / `MEMORY.md` global via config (Task 8), tests-don't-write-real-HERMES_HOME (uses `tmp_path` + wrapper). All spec sections map to a task.
- **Placeholder scan:** none — every code step shows complete code.
- **Type consistency:** `_bundle_for_current_scope()` returns `(store, retriever)` everywhere; `_resolve_scope()` returns `(kind, id)`; `_db_path_for_scope(scope)` takes that tuple; `_auto_extract_facts(store, messages)` signature updated at definition and call site. Consistent across tasks.
