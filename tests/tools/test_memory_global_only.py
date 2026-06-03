"""Tests for the global-only memory mode (user-profile store disabled).

When user_profile_enabled is False, the built-in `memory` tool is restricted to
the global, always-on `memory` target: the `user` target is dropped from the
advertised schema and `user`-target writes are refused. Per-user memory is then
handled by the scoped fact_store provider. Default (True) preserves upstream
behavior.
"""

import json

from tools.memory_tool import (
    MEMORY_SCHEMA,
    MemoryStore,
    memory_schema_for,
    memory_tool,
)


class TestUserTargetGating:
    def test_user_write_rejected_when_disabled(self):
        store = MemoryStore(user_profile_enabled=False)
        out = memory_tool(action="add", target="user", content="User prefers dark mode", store=store)
        res = json.loads(out)
        assert res.get("success") is False
        assert store.user_entries == []  # nothing written

    def test_memory_write_works_when_disabled(self):
        store = MemoryStore(user_profile_enabled=False)
        out = memory_tool(action="add", target="memory", content="VM uses the Drive SA plugin", store=store)
        res = json.loads(out)
        assert res.get("success") is True
        assert any("Drive SA" in e for e in store.memory_entries)

    def test_user_write_allowed_by_default(self):
        # Backward compat: default store still accepts the user target.
        store = MemoryStore()
        out = memory_tool(action="add", target="user", content="User is an analytics PM", store=store)
        res = json.loads(out)
        assert res.get("success") is True
        assert any("analytics PM" in e for e in store.user_entries)

    def test_user_replace_and_remove_rejected_when_disabled(self):
        store = MemoryStore(user_profile_enabled=False)
        for action, kwargs in (
            ("replace", {"old_text": "x", "content": "y"}),
            ("remove", {"old_text": "x"}),
        ):
            out = memory_tool(action=action, target="user", store=store, **kwargs)
            assert json.loads(out).get("success") is False


class TestMemorySchemaFor:
    def test_enabled_returns_default_schema(self):
        assert memory_schema_for(True) is MEMORY_SCHEMA
        assert memory_schema_for(True)["parameters"]["properties"]["target"]["enum"] == ["memory", "user"]

    def test_disabled_drops_user_target(self):
        schema = memory_schema_for(False)
        assert schema["parameters"]["properties"]["target"]["enum"] == ["memory"]
        # Description reframed to global-only and points per-user work at fact_store.
        desc = schema["description"].lower()
        assert "global" in desc
        assert "fact_store" in desc
        assert "every user" in desc or "all users" in desc.replace("all\n", "all ")

    def test_disabled_does_not_mutate_static_schema(self):
        _ = memory_schema_for(False)
        # The module-level static schema must be untouched (upstream contract).
        assert MEMORY_SCHEMA["parameters"]["properties"]["target"]["enum"] == ["memory", "user"]


class TestRenderedHeader:
    def test_global_header_when_disabled(self):
        store = MemoryStore(user_profile_enabled=False)
        block = store._render_block("memory", ["a global fact"])
        assert "GLOBAL MEMORY" in block
        assert "personal notes" not in block

    def test_default_header_unchanged(self):
        store = MemoryStore()  # user_profile_enabled defaults True
        block = store._render_block("memory", ["a note"])
        assert "MEMORY (your personal notes)" in block
