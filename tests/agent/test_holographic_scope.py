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
