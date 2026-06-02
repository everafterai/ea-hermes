"""Security regression: /resume must be scoped to the requester's identity.

The gateway `/resume` slash command previously listed titled sessions filtered
only by `source` and loaded ANY session by id/title with no identity check, so
an ordinary user could list and resume ANOTHER user's session. These tests
exercise the REAL handler against a real (tmp) SessionDB so the actual gate
code path runs: list_sessions_rich(scope=...) and the
get_session + session_row_visible target gate.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_state import SessionDB


def _source(user_id: str, chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK,
        user_id=user_id,
        chat_id=chat_id,
        user_name=user_id,
        chat_type="dm",
    )


def _event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m1")


def _entry(session_id: str, source: SessionSource) -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(source),
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
    )


def _make_runner(db: SessionDB, current_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    # Real DB so the actual gate (get_session + session_row_visible) runs.
    runner._session_db = db
    # Mock only the session-store / boundary plumbing.
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = current_entry
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "hi"}
    ]
    runner._release_running_agent_state = MagicMock()
    runner._clear_session_boundary_security_state = MagicMock()
    runner._evict_cached_agent = MagicMock()
    return runner


def _seed(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    # Alice's titled DM session.
    db.create_session("s_alice", source="slack", user_id="U_ALICE", chat_id="D_ALICE", chat_type="dm")
    db.append_message("s_alice", role="user", content="alice private note")
    db.set_session_title("s_alice", "Alice Secret Work")
    # Bob's titled DM session.
    db.create_session("s_bob", source="slack", user_id="U_BOB", chat_id="D_BOB", chat_type="dm")
    db.append_message("s_bob", role="user", content="bob note")
    db.set_session_title("s_bob", "Bob Work")
    db._conn.commit()
    return db


@pytest.mark.asyncio
async def test_bob_cannot_resume_alices_session_by_id(tmp_path):
    db = _seed(tmp_path)
    bob = _source("U_BOB", "D_BOB")
    runner = _make_runner(db, _entry("s_bob_current", bob))
    # switch_session would only be reached on a successful (in-scope) resume.
    runner.session_store.switch_session.return_value = _entry("s_alice", bob)

    result = await runner._handle_resume_command(_event("/resume s_alice", bob))

    # Out-of-scope target must be reported as not found (no existence disclosure)
    # and the actual session switch must NOT have happened. Mirror the exact
    # gateway.resume.not_found wording so out-of-scope == non-existent.
    assert "No session found matching" in result
    runner.session_store.switch_session.assert_not_called()


@pytest.mark.asyncio
async def test_bob_cannot_resume_alices_session_by_title(tmp_path):
    db = _seed(tmp_path)
    bob = _source("U_BOB", "D_BOB")
    runner = _make_runner(db, _entry("s_bob_current", bob))
    runner.session_store.switch_session.return_value = _entry("s_alice", bob)

    result = await runner._handle_resume_command(_event("/resume Alice Secret Work", bob))

    assert "couldn't find" in result.lower() or "not found" in result.lower() or "no session" in result.lower()
    runner.session_store.switch_session.assert_not_called()


@pytest.mark.asyncio
async def test_bob_can_resume_his_own_session(tmp_path):
    db = _seed(tmp_path)
    bob = _source("U_BOB", "D_BOB")
    runner = _make_runner(db, _entry("s_bob_current", bob))
    runner.session_store.switch_session.return_value = _entry("s_bob", bob)

    result = await runner._handle_resume_command(_event("/resume s_bob", bob))

    # Positive case: the real switch path runs.
    runner.session_store.switch_session.assert_called_once()
    switched_to = runner.session_store.switch_session.call_args.args[1]
    assert switched_to == "s_bob"


@pytest.mark.asyncio
async def test_listing_excludes_other_users_titled_sessions(tmp_path):
    db = _seed(tmp_path)
    bob = _source("U_BOB", "D_BOB")
    runner = _make_runner(db, _entry("s_bob_current", bob))

    # No-arg /resume lists titled sessions visible to Bob's scope only.
    result = await runner._handle_resume_command(_event("/resume", bob))

    assert "Bob Work" in result
    assert "Alice Secret Work" not in result
