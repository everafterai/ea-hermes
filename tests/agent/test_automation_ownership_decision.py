"""check_edit truth table + ownership mutations."""
import agent.automation_ownership as ao
from agent.automation_ownership import Identity, EditDecision

ALICE = Identity("slack", "U_ALICE", "Alice")
BOB = Identity("slack", "U_BOB", "Bob")


def _own(key="cron:j1", owner=ALICE, collaborators=()):
    ao._put_record(key, {
        "kind": "cron",
        "owner": {"platform": owner.platform, "user_id": owner.user_id, "display_name": owner.display_name},
        "collaborators": [{"platform": c.platform, "user_id": c.user_id, "display_name": c.display_name}
                          for c in collaborators],
        "source": "creator",
    })
    return key


def test_owner_allowed_silent():
    k = _own()
    r = ao.check_edit(k, ALICE)
    assert r.decision == EditDecision.OWNER and r.allowed and r.message == ""


def test_collaborator_allowed_silent():
    k = _own(collaborators=(BOB,))
    r = ao.check_edit(k, BOB)
    assert r.decision == EditDecision.COLLABORATOR and r.allowed


def test_cross_user_blocked_without_confirm():
    k = _own()
    r = ao.check_edit(k, BOB)
    assert r.decision == EditDecision.CROSS_USER and not r.allowed
    assert "Alice" in r.message and "confirm_cross_user_owner" in r.message


def test_cross_user_allowed_with_matching_display_name():
    k = _own()
    r = ao.check_edit(k, BOB, confirm="Alice")
    assert r.decision == EditDecision.CROSS_USER and r.allowed


def test_cross_user_allowed_with_matching_user_id():
    k = _own()
    r = ao.check_edit(k, BOB, confirm="U_ALICE")
    assert r.allowed


def test_cross_user_wrong_confirm_still_blocked():
    k = _own()
    r = ao.check_edit(k, BOB, confirm="Carol")
    assert not r.allowed


def test_unowned_allowed_with_claim_nudge():
    r = ao.check_edit("cron:unknown", BOB)
    assert r.decision == EditDecision.UNOWNED and r.allowed
    assert "claim" in r.message.lower()


def test_unowned_no_identity_allowed_silent():
    r = ao.check_edit("cron:unknown", None)
    assert r.allowed and r.message == ""


def test_owned_no_identity_blocked():
    k = _own()
    r = ao.check_edit(k, None)
    assert r.decision == EditDecision.NO_IDENTITY and not r.allowed


def test_register_creator_sets_owner_once():
    ao.register_creator("skill:s1", "skill", ALICE)
    assert ao.get_record("skill:s1")["owner"]["user_id"] == "U_ALICE"
    # second call by Bob must NOT overwrite an existing owner
    ao.register_creator("skill:s1", "skill", BOB)
    assert ao.get_record("skill:s1")["owner"]["user_id"] == "U_ALICE"


def test_register_creator_noop_without_identity():
    ao.register_creator("skill:s2", "skill", None)
    assert ao.get_record("skill:s2") is None


def test_claim_assigns_owner():
    rec = ao.claim("script:foo.sh", "script", BOB)
    assert rec["owner"]["user_id"] == "U_BOB" and rec["source"] == "claim"


def test_claim_raises_if_already_owned():
    _own(key="cron:claimed-j1", owner=ALICE)
    try:
        ao.claim("cron:claimed-j1", "cron", BOB)
        assert False, "expected PermissionError"
    except PermissionError as e:
        assert "already owned" in str(e)


def test_transfer_by_owner():
    k = _own()
    rec = ao.transfer(k, BOB, by=ALICE)
    assert rec["owner"]["user_id"] == "U_BOB"


def test_transfer_by_non_owner_non_admin_raises():
    k = _own()
    try:
        ao.transfer(k, BOB, by=BOB)
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_transfer_by_admin_allowed():
    k = _own()
    rec = ao.transfer(k, BOB, by=Identity("slack", "U_ADMIN", "Admin"), by_is_admin=True)
    assert rec["owner"]["user_id"] == "U_BOB"


def test_add_and_remove_collaborator():
    k = _own()
    ao.add_collaborator(k, BOB)
    assert any(c["user_id"] == "U_BOB" for c in ao.get_record(k)["collaborators"])
    ao.remove_collaborator(k, "U_BOB")
    assert all(c["user_id"] != "U_BOB" for c in ao.get_record(k)["collaborators"])


def test_list_for_user():
    ao._put_record("cron:a", {"kind": "cron", "owner": {"platform": "slack", "user_id": "U_ALICE", "display_name": "Alice"}, "collaborators": []})
    ao._put_record("cron:b", {"kind": "cron", "owner": {"platform": "slack", "user_id": "U_BOB", "display_name": "Bob"}, "collaborators": [{"platform": "slack", "user_id": "U_ALICE", "display_name": "Alice"}]})
    out = ao.list_for_user("U_ALICE")
    assert "cron:a" in out["owned"] and "cron:b" in out["collaborator"]
