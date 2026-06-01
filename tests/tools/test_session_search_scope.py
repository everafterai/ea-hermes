from gateway.session_context import set_session_vars, clear_session_vars
from tools.session_search_tool import resolve_search_scope


def test_scope_channel_for_shared_slack(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="slack", chat_type="group", chat_id="C123", user_id="U1")
    try:
        scope = resolve_search_scope()
    finally:
        clear_session_vars(tokens)
    assert scope == {"kind": "channel", "platform": "slack", "chat_id": "C123"}


def test_scope_user_for_dm(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="telegram", chat_type="dm", chat_id="D1", user_id="U9")
    try:
        scope = resolve_search_scope()
    finally:
        clear_session_vars(tokens)
    assert scope == {"kind": "user", "platform": "telegram", "user_id": "U9"}


def test_scope_admin_when_no_platform(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_TYPE", raising=False)
    assert resolve_search_scope() is None


def test_scope_fail_closed_when_identity_unresolvable(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="slack", chat_type="group", chat_id="", user_id="")
    try:
        scope = resolve_search_scope()
    finally:
        clear_session_vars(tokens)
    assert scope == {"kind": "none"}


def test_scope_channel_wins_when_user_id_also_present(monkeypatch):
    # A shared channel with a user_id present must still scope to the CHANNEL,
    # not the user — this is the Slack "whole channel is shared" rule.
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="slack", chat_type="group", chat_id="C123", user_id="U_PRESENT")
    try:
        scope = resolve_search_scope()
    finally:
        clear_session_vars(tokens)
    assert scope == {"kind": "channel", "platform": "slack", "chat_id": "C123"}


import json
import time
from hermes_state import SessionDB
from tools.session_search_tool import session_search


def _seed_two_users(db):
    db.create_session("s_alice", source="slack", user_id="U_ALICE", chat_id="D_ALICE", chat_type="dm")
    db.append_message("s_alice", role="user", content="my secret project codename is bluebird")
    db.create_session("s_bob", source="slack", user_id="U_BOB", chat_id="D_BOB", chat_type="dm")
    db.append_message("s_bob", role="user", content="bob asks about bluebird too")
    db._conn.commit()


def test_dm_search_does_not_leak_other_users(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_two_users(db)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="slack", chat_type="dm", chat_id="D_BOB", user_id="U_BOB")
    try:
        out = session_search(query="bluebird", db=db)
    finally:
        clear_session_vars(tokens)
    result = json.loads(out)
    sids = {r["session_id"] for r in result["results"]}
    assert "s_alice" not in sids


def test_channel_search_sees_whole_channel(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s_u1", source="slack", user_id="U1", chat_id="C1", chat_type="group")
    db.append_message("s_u1", role="user", content="channel topic: migrate the warehouse")
    db.create_session("s_u2", source="slack", user_id="U2", chat_id="C1", chat_type="group")
    db.append_message("s_u2", role="user", content="another warehouse thread here")
    db.create_session("s_other", source="slack", user_id="U3", chat_id="C2", chat_type="group")
    db.append_message("s_other", role="user", content="warehouse in a different channel")
    db._conn.commit()
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    tokens = set_session_vars(platform="slack", chat_type="group", chat_id="C1", user_id="U2")
    try:
        out = session_search(query="warehouse", db=db, current_session_id="s_u2")
    finally:
        clear_session_vars(tokens)
    sids = {r["session_id"] for r in json.loads(out)["results"]}
    assert "s_u1" in sids
    assert "s_other" not in sids


def test_admin_search_sees_all(tmp_path, monkeypatch):
    # No gateway identity -> admin -> sees both users.
    db = SessionDB(tmp_path / "state.db")
    _seed_two_users(db)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_TYPE", raising=False)
    out = session_search(query="bluebird", db=db)
    sids = {r["session_id"] for r in json.loads(out)["results"]}
    assert "s_alice" in sids and "s_bob" in sids
