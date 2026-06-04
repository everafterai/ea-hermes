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
            assert "analytics pm" in captured["prompt"].lower()
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
