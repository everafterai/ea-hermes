"""Tests for plugins/google_drive_sa/access.py — per-user Drive access check."""

from __future__ import annotations

import pytest

from plugins.google_drive_sa import access


def _cfg(**over):
    base = dict(
        enabled=True,
        cache_ttl=300.0,
        everyone_groups=frozenset({"all@everafter.ai"}),
        group_members={"sales@everafter.ai": frozenset({"alice@everafter.ai"})},
    )
    base.update(over)
    return access.AccessConfig(**base)


ALICE = "alice@everafter.ai"


# --------------------------------------------------------------------------- #
# evaluate()
# --------------------------------------------------------------------------- #

def test_evaluate_user_entry_grants_role():
    acl = [{"type": "user", "emailAddress": "Alice@EverAfter.ai", "role": "reader"}]
    d = access.evaluate(acl, ALICE, _cfg())
    assert d.granted_role == "reader"
    assert d.satisfies(access.READER) is True
    assert d.satisfies(access.WRITER) is False


def test_evaluate_domain_entry_matches_requester_domain():
    acl = [{"type": "domain", "domain": "EverAfter.ai", "role": "writer"}]
    assert access.evaluate(acl, ALICE, _cfg()).granted_role == "writer"


def test_evaluate_domain_entry_does_not_match_other_domain():
    acl = [{"type": "domain", "domain": "base.ai", "role": "writer"}]
    assert access.evaluate(acl, ALICE, _cfg()).granted_role is None


def test_evaluate_anyone_entry_always_grants():
    acl = [{"type": "anyone", "role": "commenter"}]
    d = access.evaluate(acl, ALICE, _cfg())
    assert d.granted_role == "commenter"
    assert d.satisfies(access.READER) and not d.satisfies(access.WRITER)


def test_evaluate_everyone_group_grants():
    acl = [{"type": "group", "emailAddress": "ALL@everafter.ai", "role": "reader"}]
    assert access.evaluate(acl, ALICE, _cfg()).granted_role == "reader"


def test_evaluate_mapped_group_grants_only_listed_members():
    acl = [{"type": "group", "emailAddress": "sales@everafter.ai", "role": "writer"}]
    assert access.evaluate(acl, ALICE, _cfg()).granted_role == "writer"
    d = access.evaluate(acl, "bob@everafter.ai", _cfg())
    assert d.granted_role is None
    assert d.unmapped_groups == ()  # mapped group, just not a member — not "unmapped"


def test_evaluate_unmapped_group_is_ignored_and_reported():
    acl = [
        {"type": "group", "emailAddress": "finance@everafter.ai", "role": "writer"},
        {"type": "group", "emailAddress": "finance@everafter.ai", "role": "reader"},
        {"type": "group", "emailAddress": "legal@everafter.ai", "role": "reader"},
    ]
    d = access.evaluate(acl, ALICE, _cfg())
    assert d.granted_role is None
    assert d.unmapped_groups == ("finance@everafter.ai", "legal@everafter.ai")


def test_evaluate_highest_role_wins_across_entries():
    acl = [
        {"type": "user", "emailAddress": ALICE, "role": "reader"},
        {"type": "domain", "domain": "everafter.ai", "role": "writer"},
    ]
    assert access.evaluate(acl, ALICE, _cfg()).granted_role == "writer"


@pytest.mark.parametrize(
    "role,reader,writer",
    [
        ("owner", True, True),
        ("organizer", True, True),
        ("fileOrganizer", True, True),
        ("writer", True, True),
        ("commenter", True, False),
        ("reader", True, False),
        ("bogus", False, False),
    ],
)
def test_role_ladder(role, reader, writer):
    acl = [{"type": "user", "emailAddress": ALICE, "role": role}]
    d = access.evaluate(acl, ALICE, _cfg())
    assert d.satisfies(access.READER) is reader
    assert d.satisfies(access.WRITER) is writer


def test_evaluate_empty_acl_denies():
    d = access.evaluate([], ALICE, _cfg())
    assert d.granted_role is None and d.satisfies(access.READER) is False


def test_evaluate_ignores_malformed_entries():
    acl = [None, "x", {"type": "user"}, {"role": "writer"}]
    assert access.evaluate(acl, ALICE, _cfg()).granted_role is None


# --------------------------------------------------------------------------- #
# load_access_config()
# --------------------------------------------------------------------------- #

def test_load_access_config_defaults_when_block_missing(monkeypatch):
    monkeypatch.setattr(access, "_raw_config", lambda: {})
    cfg = access.load_access_config()
    assert cfg.enabled is True
    assert cfg.cache_ttl == 300.0
    assert cfg.everyone_groups == frozenset()
    assert cfg.group_members == {}


def test_load_access_config_normalises_case_and_shapes(monkeypatch):
    monkeypatch.setattr(
        access,
        "_raw_config",
        lambda: {
            "google_drive": {
                "access_check": False,
                "acl_cache_ttl_seconds": "45",
                "everyone_groups": ["All@EverAfter.ai", " staff@everafter.ai "],
                "group_members": {
                    "Sales@everafter.ai": ["Alice@EverAfter.ai", "bob@everafter.ai"],
                    "broken": "not-a-list",
                },
            }
        },
    )
    cfg = access.load_access_config()
    assert cfg.enabled is False
    assert cfg.cache_ttl == 45.0
    assert cfg.everyone_groups == frozenset({"all@everafter.ai", "staff@everafter.ai"})
    assert cfg.group_members == {
        "sales@everafter.ai": frozenset({"alice@everafter.ai", "bob@everafter.ai"}),
    }


def test_load_access_config_survives_read_error(monkeypatch):
    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(access, "_raw_config", boom)
    assert access.load_access_config().enabled is True
