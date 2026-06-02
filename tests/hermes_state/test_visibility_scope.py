import pytest
from hermes_state import (
    SessionDB,
    build_visibility_where,
    session_row_visible,
    SHARED_CHAT_TYPES,
    build_scope,
)


def test_build_visibility_where_channel():
    frag, params = build_visibility_where(
        {"kind": "channel", "platform": "slack", "chat_id": "C123"}, alias="s"
    )
    assert "s.source = ?" in frag
    assert "s.chat_id = ?" in frag
    assert "s.chat_type IN" in frag
    assert params[:2] == ["slack", "C123"]


def test_build_visibility_where_user():
    frag, params = build_visibility_where(
        {"kind": "user", "platform": "telegram", "user_id": "U9"}, alias="s"
    )
    assert frag == "(s.source = ? AND s.user_id = ? AND (s.chat_type IS NULL OR s.chat_type NOT IN (?,?)))"
    assert params == ["telegram", "U9", "group", "channel"]


def test_session_row_visible_user_scope_excludes_own_channel():
    scope = {"kind": "user", "platform": "slack", "user_id": "U1"}
    # Same user's DM session -> visible
    assert session_row_visible(
        {"source": "slack", "chat_id": "D1", "chat_type": "dm", "user_id": "U1"}, scope
    )
    # Same user's CHANNEL session -> NOT visible from DM scope (strict partition)
    assert not session_row_visible(
        {"source": "slack", "chat_id": "C1", "chat_type": "group", "user_id": "U1"}, scope
    )
    # Legacy NULL chat_type owned by the user -> still visible (treated as non-channel)
    assert session_row_visible(
        {"source": "slack", "chat_id": None, "chat_type": None, "user_id": "U1"}, scope
    )


def test_build_visibility_where_admin_is_no_clause():
    frag, params = build_visibility_where(None, alias="s")
    assert frag == ""
    assert params == []


def test_build_visibility_where_fail_closed():
    frag, params = build_visibility_where({"kind": "none"}, alias="s")
    assert frag == "0 = 1"
    assert params == []


def test_session_row_visible_channel_matches_any_user():
    scope = {"kind": "channel", "platform": "slack", "chat_id": "C123"}
    assert session_row_visible(
        {"source": "slack", "chat_id": "C123", "chat_type": "group", "user_id": "U1"}, scope
    )
    assert session_row_visible(
        {"source": "slack", "chat_id": "C123", "chat_type": "group", "user_id": "U2"}, scope
    )
    assert not session_row_visible(
        {"source": "slack", "chat_id": "C999", "chat_type": "group", "user_id": "U1"}, scope
    )


def test_session_row_visible_user_scope_isolates():
    scope = {"kind": "user", "platform": "telegram", "user_id": "U1"}
    assert session_row_visible(
        {"source": "telegram", "chat_id": None, "chat_type": "dm", "user_id": "U1"}, scope
    )
    assert not session_row_visible(
        {"source": "telegram", "chat_id": None, "chat_type": "dm", "user_id": "U2"}, scope
    )


def test_session_row_visible_admin_sees_all():
    assert session_row_visible(
        {"source": "cli", "chat_id": None, "chat_type": None, "user_id": None}, None
    )


def test_session_row_visible_fail_closed_hides_everything():
    assert not session_row_visible(
        {"source": "slack", "chat_id": "C1", "chat_type": "group", "user_id": "U1"}, {"kind": "none"}
    )


def test_schema_has_scope_columns(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "chat_id" in cols
    assert "chat_type" in cols


def test_session_row_visible_dm_excluded_under_channel_scope():
    scope = {"kind": "channel", "platform": "slack", "chat_id": "C123"}
    # A DM row in the same platform must not be visible under a channel scope.
    assert not session_row_visible(
        {"source": "slack", "chat_id": "C123", "chat_type": "dm", "user_id": "U1"}, scope
    )


def test_build_visibility_where_unknown_kind_fails_closed():
    frag, params = build_visibility_where({"kind": "bogus"}, alias="s")
    assert frag == "0 = 1"
    assert params == []


def test_create_session_persists_scope(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1", source="slack", user_id="U1", chat_id="C7", chat_type="group")
    row = db.get_session("s1")
    assert row["chat_id"] == "C7"
    assert row["chat_type"] == "group"
    assert row["user_id"] == "U1"


def test_create_session_scope_defaults_none(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s2", source="cli")
    row = db.get_session("s2")
    assert row["chat_id"] is None
    assert row["chat_type"] is None


def test_backfill_session_scope(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s_old", source="slack")  # legacy row: chat_id/chat_type NULL
    db._conn.commit()
    db.backfill_session_scope([
        {"session_id": "s_old", "chat_id": "C5", "chat_type": "group"},
    ])
    row = db.get_session("s_old")
    assert row["chat_id"] == "C5"
    assert row["chat_type"] == "group"


def test_continuation_inherits_parent_scope(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="slack", user_id="U1", chat_id="C1", chat_type="group")
    db._conn.commit()
    parent = db.get_session("parent")
    # Mirror the compression/branch inheritance pattern:
    db.create_session(
        "child", source="slack", parent_session_id="parent",
        user_id=parent.get("user_id"), chat_id=parent.get("chat_id"), chat_type=parent.get("chat_type"),
    )
    child = db.get_session("child")
    assert child["user_id"] == "U1"
    assert child["chat_id"] == "C1"
    assert child["chat_type"] == "group"


def test_continuation_inherits_missing_parent_as_none(tmp_path):
    # Compression's `get_session(old) or {}` fallback: a missing parent row
    # yields {}, so .get(...) returns None for all three identity fields —
    # same as the pre-fix behavior (no regression).
    db = SessionDB(tmp_path / "state.db")
    parent = db.get_session("ghost") or {}
    assert parent == {}
    db.create_session(
        "child", source="slack",
        user_id=parent.get("user_id"), chat_id=parent.get("chat_id"), chat_type=parent.get("chat_type"),
    )
    child = db.get_session("child")
    assert child["user_id"] is None
    assert child["chat_id"] is None
    assert child["chat_type"] is None


def test_backfill_does_not_clobber_existing(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s_new", source="slack", chat_id="C1", chat_type="group")
    db._conn.commit()
    db.backfill_session_scope([
        {"session_id": "s_new", "chat_id": "WRONG", "chat_type": "dm"},
    ])
    row = db.get_session("s_new")
    assert row["chat_id"] == "C1"  # untouched; only NULLs are filled
    assert row["chat_type"] == "group"


def test_build_scope_channel():
    assert build_scope("slack", "group", "C1", "U1") == {"kind": "channel", "platform": "slack", "chat_id": "C1"}


def test_build_scope_user_for_dm():
    assert build_scope("telegram", "dm", "D1", "U9") == {"kind": "user", "platform": "telegram", "user_id": "U9"}


def test_build_scope_admin_when_no_platform():
    assert build_scope("", "dm", "", "") is None


def test_build_scope_fail_closed():
    assert build_scope("slack", "group", "", "") == {"kind": "none"}
