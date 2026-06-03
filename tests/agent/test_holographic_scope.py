"""Per-scope isolation tests for the holographic memory provider."""

import json
from pathlib import Path
import pytest

from gateway.session_context import set_session_vars, clear_session_vars
from plugins.memory.holographic import (
    HolographicMemoryProvider,
    _resolve_scope,
    _sanitize_scope_id,
)


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


class TestSanitizeScopeId:
    def test_already_safe_id_unchanged(self):
        scope_id = "U_SHAI123"
        assert _sanitize_scope_id(scope_id) == scope_id

    def test_special_chars_replaced_and_hash_appended(self):
        scope_id = "chat/../x"
        result = _sanitize_scope_id(scope_id)
        # Result must be safe (only [A-Za-z0-9_-] chars)
        import re
        assert re.fullmatch(r"[A-Za-z0-9_-]+", result), f"Unsafe chars in: {result!r}"
        # Must differ from a naive replace (i.e., has hash suffix)
        naive = re.sub(r"[^A-Za-z0-9_-]", "_", scope_id)
        assert result != naive, "Expected hash suffix to be appended"
        # Hash suffix: ends with underscore + 8 hex chars
        assert re.search(r"_[0-9a-f]{8}$", result), f"No 8-hex suffix in: {result!r}"

    def test_long_id_truncated_and_hash_suffixed(self):
        scope_id = "a" * 100
        result = _sanitize_scope_id(scope_id)
        # Implementation: safe[:48] + "_" + sha1[:8]  => 48 + 1 + 8 = 57 chars
        assert len(result) == 57, f"Expected 57, got {len(result)}"
        import re
        assert re.search(r"_[0-9a-f]{8}$", result), f"No 8-hex suffix in: {result!r}"


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


class TestConfigSchema:
    def test_schema_advertises_scope_keys(self):
        p = HolographicMemoryProvider(config={})
        keys = {entry["key"] for entry in p.get_config_schema()}
        assert "scope_isolation" in keys
        assert "db_dir" in keys
