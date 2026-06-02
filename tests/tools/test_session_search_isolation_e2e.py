"""End-to-end multi-user isolation guard for session_search.

Locks the contract: DMs are private to (platform, user_id); Slack channels are
shared across everyone in the same (platform, chat_id); cross-channel and
channel<->DM never leak; local CLI/admin (no identity) sees all.
"""
import json
import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search
from gateway.session_context import set_session_vars, clear_session_vars


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    # Alice DM
    d.create_session("alice_dm", source="slack", user_id="ALICE", chat_id="DA", chat_type="dm")
    d.append_message("alice_dm", role="user", content="acme launch plan is codename falcon")
    # Bob DM
    d.create_session("bob_dm", source="slack", user_id="BOB", chat_id="DB", chat_type="dm")
    d.append_message("bob_dm", role="user", content="bob notes about falcon rumor")
    # Shared channel C1 (Alice + Carol, separate sessions)
    d.create_session("c1_alice", source="slack", user_id="ALICE", chat_id="C1", chat_type="group")
    d.append_message("c1_alice", role="user", content="falcon rollout in the channel")
    d.create_session("c1_carol", source="slack", user_id="CAROL", chat_id="C1", chat_type="group")
    d.append_message("c1_carol", role="user", content="carol replies about falcon in channel")
    # Other channel C2
    d.create_session("c2_dave", source="slack", user_id="DAVE", chat_id="C2", chat_type="group")
    d.append_message("c2_dave", role="user", content="falcon in a different channel")
    d._conn.commit()
    return d


def _clear_identity(monkeypatch):
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_USER_ID",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_CHAT_TYPE",
    ):
        monkeypatch.delenv(name, raising=False)


def _search(db, query, monkeypatch, **vars_):
    _clear_identity(monkeypatch)
    tokens = set_session_vars(**vars_)
    try:
        out = session_search(query=query, db=db, limit=10)
    finally:
        clear_session_vars(tokens)
    return {r["session_id"] for r in json.loads(out)["results"]}


def test_dm_isolation(db, monkeypatch):
    # Bob in his DM searching "falcon" sees only his own DM.
    sids = _search(db, "falcon", monkeypatch,
                   platform="slack", chat_type="dm", chat_id="DB", user_id="BOB")
    assert sids == {"bob_dm"}


def test_channel_shared_visibility(db, monkeypatch):
    # Carol searching from channel C1 sees BOTH C1 sessions (hers + Alice's),
    # but not DMs and not the other channel C2.
    sids = _search(db, "falcon", monkeypatch,
                   platform="slack", chat_type="group", chat_id="C1", user_id="CAROL")
    assert "c1_alice" in sids
    assert "c1_carol" in sids
    assert "alice_dm" not in sids
    assert "bob_dm" not in sids
    assert "c2_dave" not in sids


def test_dm_does_not_see_channel(db, monkeypatch):
    # Alice in her DM must not see the channel sessions she also participated in.
    sids = _search(db, "falcon", monkeypatch,
                   platform="slack", chat_type="dm", chat_id="DA", user_id="ALICE")
    assert sids == {"alice_dm"}


def test_cli_admin_sees_all(db, monkeypatch):
    # No identity at all -> admin -> sees every session.
    _clear_identity(monkeypatch)
    out = session_search(query="falcon", db=db, limit=10)
    sids = {r["session_id"] for r in json.loads(out)["results"]}
    assert {"alice_dm", "bob_dm", "c1_alice", "c1_carol", "c2_dave"}.issubset(sids)
