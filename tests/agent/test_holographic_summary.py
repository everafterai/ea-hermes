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
