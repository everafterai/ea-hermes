"""Tests for SessionStore.reconcile_db_scope() — Task 8 of session-search
user-isolation. Backfills chat_id/chat_type onto legacy SQLite session rows
from in-memory session origins (sessions.json) so legacy channel/DM history
stays visible to the correct scope (fail-closed on NULL scope)."""
from datetime import datetime
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionSource, SessionStore
from hermes_state import SessionDB


def _make_store(tmp_path, db):
    """Build a real SessionStore with a real SessionDB, bypassing disk load."""
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._db = db
    store._loaded = True  # _ensure_loaded is patched; entries set directly below
    store._entries = {}
    return store


def _entry(session_id: str, chat_id: str, chat_type: str) -> SessionEntry:
    now = datetime.now()
    return SessionEntry(
        session_key=f"key_{session_id}",
        session_id=session_id,
        created_at=now,
        updated_at=now,
        platform=Platform.SLACK,
        chat_type=chat_type,
        origin=SessionSource(
            platform=Platform.SLACK,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id="U1",
        ),
    )


def test_reconcile_backfills_from_entries(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s_legacy", source="slack")  # NULL chat_id/chat_type
    db._conn.commit()

    store = _make_store(tmp_path, db)
    store._entries["key_s_legacy"] = _entry("s_legacy", "C9", "channel")

    store.reconcile_db_scope()

    row = db.get_session("s_legacy")
    assert row["chat_id"] == "C9"
    assert row["chat_type"] == "channel"


def test_reconcile_does_not_clobber_populated_scope(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s_set", source="slack", chat_id="C1", chat_type="group")
    db._conn.commit()

    store = _make_store(tmp_path, db)
    # Origin disagrees with the already-populated row — must NOT overwrite.
    store._entries["key_s_set"] = _entry("s_set", "WRONG", "dm")

    store.reconcile_db_scope()

    row = db.get_session("s_set")
    assert row["chat_id"] == "C1"
    assert row["chat_type"] == "group"


def test_reconcile_skips_entries_without_origin(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s_no_origin", source="slack")
    db._conn.commit()

    store = _make_store(tmp_path, db)
    now = datetime.now()
    store._entries["key_s_no_origin"] = SessionEntry(
        session_key="key_s_no_origin",
        session_id="s_no_origin",
        created_at=now,
        updated_at=now,
        platform=Platform.SLACK,
        chat_type="dm",
        origin=None,
    )

    # Must not raise; row stays NULL since there is no origin to backfill from.
    store.reconcile_db_scope()

    row = db.get_session("s_no_origin")
    assert row["chat_id"] is None
    assert row["chat_type"] is None


def test_reconcile_noop_when_db_missing(tmp_path):
    store = _make_store(tmp_path, db=None)
    store._db = None
    store._entries["key_x"] = _entry("x", "C1", "channel")
    # No db attached — must return silently without error.
    store.reconcile_db_scope()
