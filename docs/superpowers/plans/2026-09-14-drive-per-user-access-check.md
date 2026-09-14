# Google Drive per-user access check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every `google_drive` / `google_sheets` / `google_docs` tool call checks that the *requesting user* (not the shared service account) is on the target file's ACL before the call runs; listings drop files the user can't see.

**Architecture:** A new `plugins/google_drive_sa/access.py` resolves the requester from session contextvars (or the cron job's owner), fetches + caches the file's Drive ACL as the SA, and evaluates it with a pure function that fails closed on unmapped Google Groups. A sibling `identity.py` maps a platform user id to an email (config → Slack `users.info` → write-back). Handlers call `require_access` before their API call; `drive_list_files` filters its results through the same evaluator.

**Tech Stack:** Python 3.11, google-api-python-client (lazy-installed; tests use fakes), `httpx` (already a core dep) for the one Slack call, `ruamel.yaml` round-trip writer already used by `hermes_cli/users.py`, pytest via `scripts/run_tests.sh`.

**Spec:** [docs/superpowers/specs/2026-09-14-drive-per-user-access-check-design.md](../specs/2026-09-14-drive-per-user-access-check-design.md)

## Global Constraints

- Tests run ONLY through `scripts/run_tests.sh <path>` — never bare `pytest`, never the whole suite (see CLAUDE.md).
- Tests must not write to `~/.hermes/` — the autouse fixture redirects `HERMES_HOME`; config reads go through `hermes_cli.config` helpers so they follow it.
- Fail closed: no requester in an engaged gateway session → deny; ACL fetch error → deny; unresolvable email → deny. Never fall back to "allow".
- The kill switch is `google_drive.access_check` (default `true`). When `false`, handlers are byte-for-byte the pre-feature behaviour (no audit lines, no share-on-create).
- Local CLI (`gateway.session_context.session_context_engaged()` is `False`) skips the check entirely.
- Denial text shown to the model never names groups; group names go only to the audit log.
- Config keys: `google_drive.{access_check, acl_cache_ttl_seconds, everyone_groups, group_members}` (top-level block) and `slack.user_emails` (beside `user_roles` / `user_names`). Emails/domains/group addresses are compared lowercase.
- Deviation from spec, deliberate: the cron job id travels in `cron/tool_approval_context.py` (which already carries per-run cron identity for tools) rather than a new `HERMES_CRON_JOB_ID` entry in `gateway/session_context._VAR_MAP`. Same effect, one fewer module touched.
- Deviation from spec, deliberate: new test files are flat in `tests/plugins/` next to the existing `test_google_drive_sa_plugin.py` rather than in a `tests/plugins/google_drive_sa/` sub-package.
- Commit after every task. Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File map

| file | responsibility |
|---|---|
| `plugins/google_drive_sa/access.py` (new) | `AccessConfig` + loader, `evaluate()`, `Requester` + `resolve_requester()`, ACL fetch/cache, `require_access()`, `filter_listing()`, `share_with_requester()`, audit |
| `plugins/google_drive_sa/identity.py` (new) | `resolve_email(platform, user_id)`: config map → Slack `users.info` → write-back |
| `plugins/google_drive_sa/tools.py` (modify) | `drive_list_files` filter; `drive_read_file` / `drive_upload` / `drive_create_folder` gates; `create_drive_file` share-on-create |
| `plugins/google_drive_sa/sheets_tools.py` (modify) | gates on the five sheets handlers |
| `plugins/google_drive_sa/docs_tools.py` (modify) | gates on the four docs handlers |
| `cron/tool_approval_context.py` (modify) | carry `job_id`; `get_cron_job_id()` |
| `cron/scheduler.py` (modify) | pass `job_id` into `set_cron_tool_context` |
| `hermes_cli/users.py` (modify) | `user_emails` map: `apply_set_email`, `--email` on add/update, list column, delete cleanup |
| `gateway/config.py` (modify) | bridge `user_emails` into platform `extra` |
| `tests/plugins/test_google_drive_sa_access.py` (new) | evaluator, config loader, requester, ACL cache, require_access, share |
| `tests/plugins/test_google_drive_sa_identity.py` (new) | email resolution + write-back |
| `tests/plugins/test_google_drive_sa_gated_handlers.py` (new) | every handler gated; listing filter |
| `tests/plugins/test_google_drive_sa_plugin.py` (modify) | autouse fixture disabling the check so pre-existing tests stay green |
| `tests/cron/test_tool_approval_context.py` (modify) | job id round-trip |
| `tests/cli/test_users_helpers.py` (modify) | email helpers |
| `tests/gateway/test_config.py` (modify) | `user_emails` bridge |
| `tests/test_fork_feature_inventory.py` (modify) | wiring guards |
| `CLAUDE.md` (modify) | fork docs entry |

---

### Task 1: Pure evaluator + config loader

**Files:**
- Create: `plugins/google_drive_sa/access.py`
- Test: `tests/plugins/test_google_drive_sa_access.py`

**Interfaces:**
- Produces:
  - `AccessConfig(enabled: bool, cache_ttl: float, everyone_groups: frozenset[str], group_members: dict[str, frozenset[str]])`
  - `load_access_config() -> AccessConfig` (reads the `google_drive:` block via `_raw_config()`, a monkeypatchable seam)
  - `Decision(granted_role: str | None, unmapped_groups: tuple[str, ...])` with `.satisfies(level: str) -> bool`
  - `evaluate(acl: list[dict], email: str, cfg: AccessConfig) -> Decision`
  - constants `READER = "reader"`, `WRITER = "writer"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/plugins/test_google_drive_sa_access.py
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_access.py`
Expected: FAIL — `ImportError: cannot import name 'access'` (module does not exist).

- [ ] **Step 3: Write the module**

```python
# plugins/google_drive_sa/access.py
"""Per-user access check for the Drive / Sheets / Docs tools.

The plugin acts as ONE service account, so "shared with the SA" would
otherwise mean "readable by every user whose role grants the toolset". This
module makes each tool call answer to the *requesting* user instead: resolve
who is asking (session contextvars, or the cron job's owner), fetch the
target file's ACL as the SA, and check that user is on it. Google Groups
cannot be expanded without domain-wide delegation, so a group grant counts
only when the operator has mapped it in config (``everyone_groups`` /
``group_members``); anything else fails closed and is reported to the audit
log so the operator can see which group to map.

Design: docs/superpowers/specs/2026-09-14-drive-per-user-access-check-design.md
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

READER = "reader"
WRITER = "writer"

# Drive roles that satisfy each access level. ``owner``/``organizer``/
# ``fileOrganizer`` are shared-drive and My-Drive spellings of "can edit".
_WRITER_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer"})
_READER_ROLES = _WRITER_ROLES | frozenset({"commenter", "reader"})
# Rank for "highest role wins" — higher is more privileged.
_ROLE_RANK = {
    "reader": 1,
    "commenter": 2,
    "writer": 3,
    "fileOrganizer": 4,
    "organizer": 5,
    "owner": 6,
}

_DEFAULT_TTL = 300.0


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AccessConfig:
    enabled: bool = True
    cache_ttl: float = _DEFAULT_TTL
    everyone_groups: frozenset = frozenset()
    group_members: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.group_members is None:
            object.__setattr__(self, "group_members", {})


def _raw_config() -> dict:
    """Seam: the raw ``config.yaml`` dict. Tests monkeypatch this."""
    from hermes_cli.config import read_raw_config_readonly

    return read_raw_config_readonly() or {}


def _lower_str(value: Any) -> str:
    return str(value or "").strip().lower()


def load_access_config() -> AccessConfig:
    """Read the top-level ``google_drive:`` block. Defaults on any failure."""
    try:
        block = _raw_config().get("google_drive") or {}
        if not isinstance(block, dict):
            block = {}
        enabled = bool(block.get("access_check", True))
        try:
            ttl = float(block.get("acl_cache_ttl_seconds", _DEFAULT_TTL))
        except (TypeError, ValueError):
            ttl = _DEFAULT_TTL
        raw_everyone = block.get("everyone_groups") or []
        everyone = frozenset(
            _lower_str(g) for g in raw_everyone if _lower_str(g)
        ) if isinstance(raw_everyone, (list, tuple, set)) else frozenset()
        members: dict[str, frozenset] = {}
        raw_members = block.get("group_members") or {}
        if isinstance(raw_members, dict):
            for group, emails in raw_members.items():
                if not isinstance(emails, (list, tuple, set)):
                    continue
                key = _lower_str(group)
                if key:
                    members[key] = frozenset(
                        _lower_str(e) for e in emails if _lower_str(e)
                    )
        return AccessConfig(
            enabled=enabled,
            cache_ttl=ttl,
            everyone_groups=everyone,
            group_members=members,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("google_drive access config unreadable (%s); using defaults", exc)
        return AccessConfig()


# --------------------------------------------------------------------------- #
# Evaluation (pure)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Decision:
    granted_role: Optional[str]
    unmapped_groups: tuple = ()

    def satisfies(self, level: str) -> bool:
        if self.granted_role is None:
            return False
        if level == WRITER:
            return self.granted_role in _WRITER_ROLES
        return self.granted_role in _READER_ROLES


def evaluate(acl: list, email: str, cfg: AccessConfig) -> Decision:
    """Pure ACL evaluation for one requester. Never raises on bad entries."""
    e = _lower_str(email)
    domain = e.rsplit("@", 1)[1] if "@" in e else ""
    best: Optional[str] = None
    unmapped: list[str] = []
    for entry in acl or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "")
        if role not in _ROLE_RANK:
            continue
        kind = str(entry.get("type") or "")
        matched = False
        if kind == "user":
            matched = _lower_str(entry.get("emailAddress")) == e and bool(e)
        elif kind == "domain":
            matched = bool(domain) and _lower_str(entry.get("domain")) == domain
        elif kind == "anyone":
            matched = True
        elif kind == "group":
            group = _lower_str(entry.get("emailAddress"))
            if group in cfg.everyone_groups:
                matched = True
            elif group in cfg.group_members:
                matched = e in cfg.group_members[group]
            elif group and group not in unmapped:
                unmapped.append(group)
        if matched and (best is None or _ROLE_RANK[role] > _ROLE_RANK[best]):
            best = role
    return Decision(granted_role=best, unmapped_groups=tuple(unmapped))
```

- [ ] **Step 4: Run to verify they pass**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_access.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/google_drive_sa/access.py tests/plugins/test_google_drive_sa_access.py
git commit -m "feat(gdrive): pure ACL evaluator + access config for per-user check

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Cron job id available to tools

**Files:**
- Modify: `cron/tool_approval_context.py`
- Modify: `cron/scheduler.py:587-601` (`_enter_cron_tool_context`)
- Test: `tests/cron/test_tool_approval_context.py`

**Interfaces:**
- Produces: `set_cron_tool_context(*, owner_grant, acked_tools, job_id=None)`, `get_cron_job_id() -> str | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/cron/test_tool_approval_context.py`:

```python
from cron.tool_approval_context import get_cron_job_id


def test_cron_tool_context_carries_job_id():
    assert get_cron_job_id() is None
    tok = set_cron_tool_context(owner_grant=None, acked_tools=[], job_id="job-42")
    try:
        assert get_cron_job_id() == "job-42"
    finally:
        clear_cron_tool_context(tok)
    assert get_cron_job_id() is None


def test_cron_tool_context_job_id_defaults_to_none():
    tok = set_cron_tool_context(owner_grant=None, acked_tools=[])
    try:
        assert get_cron_job_id() is None
    finally:
        clear_cron_tool_context(tok)
```

- [ ] **Step 2: Run to verify it fails**

Run: `scripts/run_tests.sh tests/cron/test_tool_approval_context.py`
Expected: FAIL — `ImportError: cannot import name 'get_cron_job_id'`.

- [ ] **Step 3: Implement**

Replace the body of `cron/tool_approval_context.py` from `_owner_grant = ...` down with:

```python
_owner_grant: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "cron_tool_owner_grant", default=None)
_acked_tools: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "cron_tool_acked", default=None)
_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "cron_tool_active", default=False)
# The running job's id. Lets a tool that needs *who* is behind this run (the
# Drive per-user access check resolves the job's owner from the ownership
# registry) find it without the scheduler threading the job dict through.
_job_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "cron_tool_job_id", default=None)


def set_cron_tool_context(*, owner_grant, acked_tools, job_id=None):
    t1 = _owner_grant.set(frozenset(owner_grant) if owner_grant is not None else None)
    t2 = _acked_tools.set(frozenset(acked_tools or ()))
    t3 = _active.set(True)
    t4 = _job_id.set(str(job_id) if job_id else None)
    return (t1, t2, t3, t4)


def clear_cron_tool_context(token) -> None:
    t1, t2, t3, t4 = token
    _owner_grant.reset(t1)
    _acked_tools.reset(t2)
    _active.reset(t3)
    _job_id.reset(t4)


def get_cron_tool_context() -> Tuple[Optional[frozenset], frozenset]:
    return _owner_grant.get(), (_acked_tools.get() or frozenset())


def get_cron_job_id() -> Optional[str]:
    return _job_id.get()


def in_cron_run() -> bool:
    return bool(_active.get())
```

In `cron/scheduler.py` `_enter_cron_tool_context`, change the last line:

```python
    return set_cron_tool_context(owner_grant=grant, acked_tools=acked, job_id=job.get("id"))
```

- [ ] **Step 4: Run to verify it passes**

Run: `scripts/run_tests.sh tests/cron/test_tool_approval_context.py tests/cron/test_rbac_ceiling.py`
Expected: all PASS (the second file exercises the scheduler helper; if it doesn't exist under that name, run `scripts/run_tests.sh tests/cron/ -k "approval or ceiling"`).

- [ ] **Step 5: Commit**

```bash
git add cron/tool_approval_context.py cron/scheduler.py tests/cron/test_tool_approval_context.py
git commit -m "feat(cron): expose the running job id to tools via tool_approval_context

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `slack.user_emails` — CLI helpers, config bridge

**Files:**
- Modify: `hermes_cli/users.py` (helpers `_user_emails`, `apply_set_email`; `email` param on `apply_add`/`apply_update`; cleanup in `apply_delete`; `--email` flag; list column)
- Modify: `gateway/config.py:1751-1760` (bridge `user_emails` after `user_names`)
- Test: `tests/cli/test_users_helpers.py`, `tests/gateway/test_config.py`

**Interfaces:**
- Produces: `hermes_cli.users.apply_set_email(extra: dict, user_id: str, email: str) -> MutationResult` (does NOT require the user to be in `user_roles`); `apply_add(extra, user_id, role, name, email=None)`; `apply_update(extra, user_id, role, name, email=None)`; `_mutate_slack(mutator)` unchanged (Task 4 reuses it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_users_helpers.py`:

```python
from hermes_cli.users import apply_set_email


def test_apply_set_email_creates_map_without_requiring_role():
    extra = {}
    apply_set_email(extra, "U1", "Alice@EverAfter.ai")
    assert extra["user_emails"] == {"U1": "alice@everafter.ai"}
    assert "user_roles" not in extra


def test_apply_set_email_rejects_non_email():
    extra = {}
    with pytest.raises(UsersError):
        apply_set_email(extra, "U1", "not-an-email")


def test_apply_add_with_email():
    extra = {}
    apply_add(extra, "U1", "operator", None, email="a@x.io")
    assert extra["user_emails"] == {"U1": "a@x.io"}


def test_apply_update_email_only():
    extra = {"user_roles": {"U1": "operator"}}
    apply_update(extra, "U1", None, None, email="a@x.io")
    assert extra["user_emails"] == {"U1": "a@x.io"}
    assert extra["user_roles"] == {"U1": "operator"}


def test_apply_delete_removes_email():
    extra = {"user_roles": {"U1": "operator"}, "user_emails": {"U1": "a@x.io"}}
    apply_delete(extra, "U1")
    assert extra["user_emails"] == {}
```

(Ensure `pytest`, `UsersError`, `apply_add`, `apply_update`, `apply_delete` are already imported at the top of that file; add any that are missing.)

Add to `tests/gateway/test_config.py`, right after `test_bridges_user_names_from_config_yaml` in the same class:

```python
    def test_bridges_user_emails_from_config_yaml(self, tmp_path, monkeypatch):
        """``user_emails`` reaches ``extra`` beside ``user_names`` (Drive per-user check)."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "slack:\n"
            "  user_roles:\n"
            "    U_ALICE: operator\n"
            "  user_emails:\n"
            "    U_ALICE: alice@everafter.ai\n"
            "    123456: bob@everafter.ai\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        extra = load_gateway_config().platforms[Platform.SLACK].extra
        assert extra["user_emails"] == {"U_ALICE": "alice@everafter.ai", "123456": "bob@everafter.ai"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/run_tests.sh tests/cli/test_users_helpers.py tests/gateway/test_config.py -k "email"`
Expected: FAIL — `ImportError` for `apply_set_email`; `KeyError: 'user_emails'`.

- [ ] **Step 3: Implement in `hermes_cli/users.py`**

Add after `_user_names`:

```python
def _user_emails(extra: Dict[str, Any]) -> Dict[str, Any]:
    existing = _as_map(extra.get("user_emails"), "user_emails")
    if existing is None:
        existing = {}
        extra["user_emails"] = existing
    return existing


def _canonical_email(email: str) -> str:
    canon = str(email or "").strip().lower()
    if "@" not in canon or canon.startswith("@") or canon.endswith("@"):
        raise UsersError(f"{email!r} is not an email address")
    return canon


def apply_set_email(extra: Dict[str, Any], user_id: str, email: str) -> MutationResult:
    """Record ``user_id → email`` in ``slack.user_emails``.

    Deliberately does NOT require the user to be in ``user_roles``: the Drive
    per-user access check writes back emails it resolved from Slack for users
    who already passed the message gate, and RBAC may be off entirely.
    """
    _user_emails(extra)[user_id] = _canonical_email(email)
    return MutationResult()
```

Change the signatures and bodies of `apply_add` / `apply_update`:

```python
def apply_add(
    extra: Dict[str, Any],
    user_id: str,
    role: str,
    name: Optional[str],
    email: Optional[str] = None,
) -> MutationResult:
    """Add a brand-new user with ``role`` (and optional ``name`` / ``email``)."""
    role = _canonical_role(extra, role)
    user_roles = _user_roles(extra)
    if user_id in user_roles:
        raise UsersError(
            f"user {user_id!r} already exists; use `update` to change it"
        )
    rbac_activated = len(user_roles) == 0
    user_roles[user_id] = role
    if name is not None:
        _user_names(extra)[user_id] = name
    if email is not None:
        _user_emails(extra)[user_id] = _canonical_email(email)
    added, removed = _sync_admin(extra, user_id, role)
    return MutationResult(
        rbac_activated=rbac_activated,
        admin_added=added,
        admin_removed=removed,
    )


def apply_update(
    extra: Dict[str, Any],
    user_id: str,
    role: Optional[str],
    name: Optional[str],
    email: Optional[str] = None,
) -> MutationResult:
    """Update an existing user's ``role`` and/or ``name`` and/or ``email``."""
    user_roles = _user_roles(extra)
    if user_id not in user_roles:
        raise UsersError(f"user {user_id!r} does not exist; use `add` to create it")
    if role is None and name is None and email is None:
        raise UsersError("nothing to update; pass a role, --name and/or --email")
    result = MutationResult()
    if role is not None:
        role = _canonical_role(extra, role)
        user_roles[user_id] = role
        added, removed = _sync_admin(extra, user_id, role)
        result.admin_added = added
        result.admin_removed = removed
    if name is not None:
        _user_names(extra)[user_id] = name
    if email is not None:
        _user_emails(extra)[user_id] = _canonical_email(email)
    return result
```

In `apply_delete`, after the `user_names` cleanup:

```python
    emails = extra.get("user_emails")
    if isinstance(emails, dict) and user_id in emails:
        del emails[user_id]
```

Handlers: in `handle_users_add` and `handle_users_update`, read `email = getattr(args, "email", None)` and pass it as the fifth argument to `apply_add` / `apply_update`; include `email=...` in the printed detail when set. In `handle_users_list`, read `user_emails = _as_map(extra.get("user_emails"), "user_emails") or {}`, add `"email": user_emails.get(user_id) or ""` to each row, print an `EMAIL` column between `NAME` and `ROLE` (compute `email_w` like `name_w`).

Parser: add `p_add.add_argument("--email", default=None, help="Google Workspace email (Drive per-user access check)")` and the same on `p_update`. Update the module docstring's key list with `user_emails`.

- [ ] **Step 4: Implement in `gateway/config.py`**

Right after the `user_names` bridge block (ends at line ~1760), add:

```python
                if "user_emails" in platform_cfg:
                    # Drive per-user access check: platform user id → Google
                    # Workspace email. Same stringification as user_names.
                    user_emails = platform_cfg["user_emails"]
                    if isinstance(user_emails, dict):
                        bridged["user_emails"] = {str(k): v for k, v in user_emails.items()}
                    else:
                        bridged["user_emails"] = user_emails
```

- [ ] **Step 5: Run to verify they pass**

Run: `scripts/run_tests.sh tests/cli/test_users_helpers.py tests/cli/test_users_cli.py tests/cli/test_users_cli_wiring.py tests/gateway/test_config.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/users.py gateway/config.py tests/cli/test_users_helpers.py tests/gateway/test_config.py
git commit -m "feat(users): slack.user_emails map — CLI --email, list column, config bridge

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Email resolution — `identity.py`

**Files:**
- Create: `plugins/google_drive_sa/identity.py`
- Test: `tests/plugins/test_google_drive_sa_identity.py`

**Interfaces:**
- Consumes: `hermes_cli.users._mutate_slack`, `hermes_cli.users.apply_set_email` (Task 3); `tools.slack_react_tool._resolve_slack_token`.
- Produces: `resolve_email(platform: str, user_id: str) -> str | None` (lowercased); seams `_raw_config()`, `_slack_users_info(user_id) -> dict`, `_persist_email(platform, user_id, email) -> None`; `reset_cache()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/plugins/test_google_drive_sa_identity.py
"""Tests for plugins/google_drive_sa/identity.py — platform user id → email."""

from __future__ import annotations

import pytest

from plugins.google_drive_sa import identity


@pytest.fixture(autouse=True)
def _reset():
    identity.reset_cache()
    yield
    identity.reset_cache()


def test_config_map_wins_and_lowercases(monkeypatch):
    monkeypatch.setattr(
        identity, "_raw_config",
        lambda: {"slack": {"user_emails": {"U1": "Alice@EverAfter.ai"}}},
    )
    calls = []
    monkeypatch.setattr(identity, "_slack_users_info", lambda uid: calls.append(uid) or {})
    assert identity.resolve_email("slack", "U1") == "alice@everafter.ai"
    assert calls == []


def test_config_map_numeric_ids_are_stringified(monkeypatch):
    monkeypatch.setattr(
        identity, "_raw_config",
        lambda: {"slack": {"user_emails": {123: "a@x.io"}}},
    )
    assert identity.resolve_email("slack", "123") == "a@x.io"


def test_slack_lookup_then_persist(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})
    monkeypatch.setattr(
        identity, "_slack_users_info",
        lambda uid: {"ok": True, "user": {"id": uid, "profile": {"email": "Bob@EverAfter.ai"}}},
    )
    persisted = []
    monkeypatch.setattr(identity, "_persist_email", lambda p, u, e: persisted.append((p, u, e)))
    assert identity.resolve_email("slack", "U2") == "bob@everafter.ai"
    assert persisted == [("slack", "U2", "bob@everafter.ai")]


def test_slack_lookup_result_cached_in_memory_even_if_persist_fails(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})
    calls = []

    def _info(uid):
        calls.append(uid)
        return {"ok": True, "user": {"profile": {"email": "c@x.io"}}}

    monkeypatch.setattr(identity, "_slack_users_info", _info)

    def _boom(p, u, e):
        raise OSError("read-only fs")

    monkeypatch.setattr(identity, "_persist_email", _boom)
    assert identity.resolve_email("slack", "U3") == "c@x.io"
    assert identity.resolve_email("slack", "U3") == "c@x.io"
    assert calls == ["U3"]  # second call hit neither Slack nor disk


def test_slack_error_response_returns_none(monkeypatch, caplog):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})
    monkeypatch.setattr(identity, "_slack_users_info", lambda uid: {"ok": False, "error": "missing_scope"})
    persisted = []
    monkeypatch.setattr(identity, "_persist_email", lambda p, u, e: persisted.append(e))
    with caplog.at_level("WARNING"):
        assert identity.resolve_email("slack", "U4") is None
    assert persisted == []
    assert "users:read.email" in caplog.text


def test_slack_exception_returns_none(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {"slack": {}})

    def _boom(uid):
        raise RuntimeError("network")

    monkeypatch.setattr(identity, "_slack_users_info", _boom)
    assert identity.resolve_email("slack", "U5") is None


def test_non_slack_platform_uses_map_but_no_api_fallback(monkeypatch):
    monkeypatch.setattr(
        identity, "_raw_config",
        lambda: {"google_chat": {"user_emails": {"users/9": "g@x.io"}}},
    )
    calls = []
    monkeypatch.setattr(identity, "_slack_users_info", lambda uid: calls.append(uid) or {})
    assert identity.resolve_email("google_chat", "users/9") == "g@x.io"
    assert identity.resolve_email("google_chat", "users/10") is None
    assert calls == []


def test_empty_identity_returns_none(monkeypatch):
    monkeypatch.setattr(identity, "_raw_config", lambda: {})
    assert identity.resolve_email("", "U1") is None
    assert identity.resolve_email("slack", "") is None


def test_persist_email_writes_config_via_users_helper(monkeypatch, tmp_path):
    """Real write-back path: ruamel round-trip through hermes_cli.users._mutate_slack."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        "# keep me\n"
        "slack:\n"
        "  user_roles:\n"
        "    U1: operator  # alice\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    identity._persist_email("slack", "U1", "alice@everafter.ai")
    text = cfg.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "# alice" in text
    assert "user_emails:" in text and "U1: alice@everafter.ai" in text


def test_persist_email_non_slack_is_noop(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg = hermes_home / "config.yaml"
    cfg.write_text("slack:\n  user_roles: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    identity._persist_email("google_chat", "users/1", "g@x.io")
    assert "user_emails" not in cfg.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_identity.py`
Expected: FAIL — `ImportError: cannot import name 'identity'`.

- [ ] **Step 3: Write the module**

```python
# plugins/google_drive_sa/identity.py
"""Platform user id → Google Workspace email, for the Drive access check.

Lookup order: ``<platform>.user_emails`` in config.yaml → (Slack only)
``users.info`` → write the answer back into ``slack.user_emails`` so the next
turn — and the next gateway restart — never asks Slack again. A resolved
email is always cached in-process regardless of whether the write-back
succeeded, so a read-only config can't cause a Slack call per turn.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_SLACK_USERS_INFO = "https://slack.com/api/users.info"

_lock = threading.Lock()
_cache: dict[tuple[str, str], str] = {}


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _raw_config() -> dict:
    """Seam: the raw ``config.yaml`` dict (cached on mtime by hermes_cli)."""
    from hermes_cli.config import read_raw_config_readonly

    return read_raw_config_readonly() or {}


def _lookup_config(platform: str, user_id: str) -> Optional[str]:
    try:
        block = _raw_config().get(platform) or {}
        emails = block.get("user_emails") if isinstance(block, dict) else None
        if not isinstance(emails, dict):
            return None
        for k, v in emails.items():
            if str(k) == user_id and v:
                return str(v).strip().lower() or None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("user_emails lookup failed: %s", exc)
    return None


def _slack_users_info(user_id: str) -> dict:
    """Seam: GET users.info. Returns the parsed JSON body (``{}`` if no token)."""
    from tools.slack_react_tool import _resolve_slack_token

    token = _resolve_slack_token()
    if not token:
        return {}
    import httpx
    from gateway.platforms.base import resolve_proxy_url

    proxy = resolve_proxy_url() or None
    with httpx.Client(timeout=10.0, proxy=proxy) as http:
        resp = http.get(
            _SLACK_USERS_INFO,
            params={"user": user_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        return resp.json()


def _lookup_slack(user_id: str) -> Optional[str]:
    try:
        data = _slack_users_info(user_id) or {}
    except Exception as exc:
        logger.warning("Slack users.info(%s) failed: %s", user_id, exc)
        return None
    if not data.get("ok"):
        err = data.get("error") or "no token"
        hint = " (add the users:read.email bot scope and reinstall the app)" if err in (
            "missing_scope", "no token"
        ) else ""
        logger.warning(
            "Slack users.info(%s) returned %s%s — cannot resolve email; "
            "set slack.user_emails.%s manually or fix the scope (users:read.email)",
            user_id, err, hint, user_id,
        )
        return None
    email = ((data.get("user") or {}).get("profile") or {}).get("email")
    email = str(email or "").strip().lower()
    return email or None


def _persist_email(platform: str, user_id: str, email: str) -> None:
    """Write ``user_id → email`` into ``<platform>.user_emails`` (Slack only in v1).

    Reuses the comment-preserving writer from ``hermes users``. Raises on
    failure — the caller logs and keeps the in-memory value.
    """
    if platform != "slack":
        return
    from hermes_cli.users import _mutate_slack, apply_set_email

    _mutate_slack(lambda extra: apply_set_email(extra, user_id, email))


def resolve_email(platform: str, user_id: str) -> Optional[str]:
    platform = str(platform or "").strip().lower()
    user_id = str(user_id or "").strip()
    if not platform or not user_id:
        return None
    key = (platform, user_id)
    with _lock:
        cached = _cache.get(key)
    if cached:
        return cached

    email = _lookup_config(platform, user_id)
    persisted = True
    if not email and platform == "slack":
        email = _lookup_slack(user_id)
        persisted = False
    if not email:
        return None

    with _lock:
        _cache[key] = email
    if not persisted:
        try:
            _persist_email(platform, user_id, email)
        except Exception as exc:
            logger.warning(
                "could not write slack.user_emails.%s to config.yaml (%s); "
                "using the in-memory value for this process", user_id, exc,
            )
    return email
```

- [ ] **Step 4: Run to verify they pass**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_identity.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/google_drive_sa/identity.py tests/plugins/test_google_drive_sa_identity.py
git commit -m "feat(gdrive): resolve requester email from config, Slack users.info, write-back

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Requester resolution, ACL fetch/cache, `require_access`, share-on-create

**Files:**
- Modify: `plugins/google_drive_sa/access.py` (append)
- Test: `tests/plugins/test_google_drive_sa_access.py` (append)

**Interfaces:**
- Consumes: `identity.resolve_email` (Task 4); `cron.tool_approval_context.get_cron_job_id` (Task 2); `agent.automation_ownership.get_record/artifact_key`; `gateway.session_context.get_session_env/session_context_engaged`; `plugins.google_drive_sa.client.get_service`; `agent.data_access_audit.record_access`.
- Produces:
  - `Requester(platform: str, user_id: str, email: str)`
  - `is_check_active() -> bool` — `load_access_config().enabled and session_context_engaged()`
  - `resolve_requester() -> Requester | None`
  - `class DriveAccessDenied(Exception)` — `str(exc)` is the model-facing message
  - `fetch_acl(file_id) -> tuple[str, list[dict]]` — `(name, acl)`, cached; raises on API error
  - `require_access(file_id: str, level: str) -> Requester | None` — `None` when the check is inactive; raises `DriveAccessDenied`
  - `share_with_requester(file_id: str, requester: Requester | None) -> str | None` — error text or `None`
  - `reset_cache()`
  - seams: `_engaged()`, `_cron_owner()`, `_now()`

- [ ] **Step 1: Write the failing tests**

Append to `tests/plugins/test_google_drive_sa_access.py`:

```python
# --------------------------------------------------------------------------- #
# Requester resolution
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_access_cache():
    access.reset_cache()
    yield
    access.reset_cache()


@pytest.fixture
def engaged(monkeypatch):
    monkeypatch.setattr(access, "_engaged", lambda: True)
    monkeypatch.setattr(access, "load_access_config", lambda: _cfg())


def _session(monkeypatch, platform="slack", user_id="U1"):
    from gateway.session_context import set_session_vars, clear_session_vars

    tokens = set_session_vars(platform=platform, user_id=user_id)
    return lambda: clear_session_vars(tokens)


def test_resolve_requester_from_session(monkeypatch, engaged):
    monkeypatch.setattr(access, "_resolve_email", lambda p, u: "alice@everafter.ai" if (p, u) == ("slack", "U1") else None)
    undo = _session(monkeypatch)
    try:
        r = access.resolve_requester()
    finally:
        undo()
    assert r == access.Requester(platform="slack", user_id="U1", email="alice@everafter.ai")


def test_resolve_requester_none_when_email_unresolvable(monkeypatch, engaged):
    monkeypatch.setattr(access, "_resolve_email", lambda p, u: None)
    undo = _session(monkeypatch)
    try:
        assert access.resolve_requester() is None
    finally:
        undo()


def test_resolve_requester_none_without_identity(monkeypatch, engaged):
    monkeypatch.setattr(access, "_resolve_email", lambda p, u: "x@y.z")
    monkeypatch.setattr(access, "_cron_owner", lambda: None)
    undo = _session(monkeypatch, platform="", user_id="")
    try:
        assert access.resolve_requester() is None
    finally:
        undo()


def test_resolve_requester_from_cron_owner(monkeypatch, engaged):
    monkeypatch.setattr(access, "_cron_owner", lambda: ("slack", "U9"))
    monkeypatch.setattr(access, "_resolve_email", lambda p, u: "owner@everafter.ai" if u == "U9" else None)
    undo = _session(monkeypatch, platform="", user_id="")
    try:
        r = access.resolve_requester()
    finally:
        undo()
    assert r.email == "owner@everafter.ai" and r.user_id == "U9"


def test_cron_owner_reads_ownership_registry(monkeypatch):
    from cron.tool_approval_context import set_cron_tool_context, clear_cron_tool_context
    from agent import automation_ownership as ao

    monkeypatch.setattr(
        ao, "get_record",
        lambda key: {"owner": {"platform": "slack", "user_id": "U7"}} if key == "cron:j1" else None,
    )
    tok = set_cron_tool_context(owner_grant=None, acked_tools=[], job_id="j1")
    try:
        assert access._cron_owner() == ("slack", "U7")
    finally:
        clear_cron_tool_context(tok)
    assert access._cron_owner() is None  # no job id → no owner


def test_cron_owner_none_for_ownerless_job(monkeypatch):
    from cron.tool_approval_context import set_cron_tool_context, clear_cron_tool_context
    from agent import automation_ownership as ao

    monkeypatch.setattr(ao, "get_record", lambda key: None)
    tok = set_cron_tool_context(owner_grant=None, acked_tools=[], job_id="j2")
    try:
        assert access._cron_owner() is None
    finally:
        clear_cron_tool_context(tok)


# --------------------------------------------------------------------------- #
# is_check_active()
# --------------------------------------------------------------------------- #

def test_check_inactive_when_not_engaged(monkeypatch):
    monkeypatch.setattr(access, "_engaged", lambda: False)
    monkeypatch.setattr(access, "load_access_config", lambda: _cfg())
    assert access.is_check_active() is False


def test_check_inactive_when_disabled(monkeypatch):
    monkeypatch.setattr(access, "_engaged", lambda: True)
    monkeypatch.setattr(access, "load_access_config", lambda: _cfg(enabled=False))
    assert access.is_check_active() is False


def test_check_active(monkeypatch, engaged):
    assert access.is_check_active() is True


# --------------------------------------------------------------------------- #
# ACL fetch + cache
# --------------------------------------------------------------------------- #

class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        if isinstance(self._r, Exception):
            raise self._r
        return self._r


class _FakeDrive:
    """files().get / permissions().list / permissions().create recorder."""

    def __init__(self, *, get=None, perms=None, perm_pages=None):
        self.get_calls = []
        self.list_calls = []
        self.create_calls = []
        self._get = get
        self._perm_pages = perm_pages if perm_pages is not None else (
            [{"permissions": perms or []}]
        )

    def files(self):
        outer = self

        class _F:
            def get(self, **kw):
                outer.get_calls.append(kw)
                return _Req(outer._get)

        return _F()

    def permissions(self):
        outer = self

        class _P:
            def list(self, **kw):
                outer.list_calls.append(kw)
                page = len(outer.list_calls) - 1
                return _Req(outer._perm_pages[min(page, len(outer._perm_pages) - 1)])

            def create(self, **kw):
                outer.create_calls.append(kw)
                return _Req({"id": "perm1"})

        return _P()


@pytest.fixture
def drive(monkeypatch):
    def _install(fake):
        from plugins.google_drive_sa import client as gd_client

        monkeypatch.setattr(gd_client, "get_service", lambda: fake)
        return fake

    return _install


def test_fetch_acl_uses_inline_permissions(drive):
    fake = drive(_FakeDrive(get={"id": "f1", "name": "Doc", "permissions": [
        {"type": "user", "emailAddress": ALICE, "role": "reader"}]}))
    name, acl = access.fetch_acl("f1")
    assert name == "Doc" and acl[0]["emailAddress"] == ALICE
    assert fake.list_calls == []
    assert fake.get_calls[0]["supportsAllDrives"] is True
    assert "permissions(" in fake.get_calls[0]["fields"]


def test_fetch_acl_falls_back_to_permissions_list_and_pages(drive):
    fake = drive(_FakeDrive(
        get={"id": "f1", "name": "SD", "driveId": "D1"},
        perm_pages=[
            {"permissions": [{"type": "user", "emailAddress": "a@x", "role": "reader"}], "nextPageToken": "p2"},
            {"permissions": [{"type": "domain", "domain": "x", "role": "writer"}]},
        ],
    ))
    _, acl = access.fetch_acl("f1")
    assert [e["type"] for e in acl] == ["user", "domain"]
    assert len(fake.list_calls) == 2
    assert fake.list_calls[1]["pageToken"] == "p2"
    assert fake.list_calls[0]["supportsAllDrives"] is True


def test_fetch_acl_cached_within_ttl(drive, monkeypatch):
    fake = drive(_FakeDrive(get={"id": "f1", "name": "Doc", "permissions": []}))
    monkeypatch.setattr(access, "load_access_config", lambda: _cfg(cache_ttl=100.0))
    t = [1000.0]
    monkeypatch.setattr(access, "_now", lambda: t[0])
    access.fetch_acl("f1")
    t[0] += 50
    access.fetch_acl("f1")
    assert len(fake.get_calls) == 1
    t[0] += 60  # past TTL
    access.fetch_acl("f1")
    assert len(fake.get_calls) == 2


def test_fetch_acl_raises_on_api_error(drive):
    drive(_FakeDrive(get=RuntimeError("403")))
    with pytest.raises(RuntimeError):
        access.fetch_acl("f1")


# --------------------------------------------------------------------------- #
# require_access()
# --------------------------------------------------------------------------- #

@pytest.fixture
def alice(monkeypatch, engaged):
    monkeypatch.setattr(
        access, "resolve_requester",
        lambda: access.Requester(platform="slack", user_id="U1", email=ALICE),
    )
    audit = []
    monkeypatch.setattr(access, "_audit_denied", lambda **kw: audit.append(kw))
    return audit


def test_require_access_returns_none_when_inactive(monkeypatch, drive):
    fake = drive(_FakeDrive(get={"id": "f1", "permissions": []}))
    monkeypatch.setattr(access, "is_check_active", lambda: False)
    assert access.require_access("f1", access.READER) is None
    assert fake.get_calls == []


def test_require_access_allows_reader(drive, alice):
    drive(_FakeDrive(get={"id": "f1", "name": "Doc", "permissions": [
        {"type": "user", "emailAddress": ALICE, "role": "reader"}]}))
    r = access.require_access("f1", access.READER)
    assert r.email == ALICE
    assert alice == []


def test_require_access_denies_writer_for_reader(drive, alice):
    drive(_FakeDrive(get={"id": "f1", "name": "Doc", "permissions": [
        {"type": "user", "emailAddress": ALICE, "role": "reader"}]}))
    with pytest.raises(access.DriveAccessDenied) as ei:
        access.require_access("f1", access.WRITER)
    assert ALICE in str(ei.value)
    assert "edit" in str(ei.value)
    assert alice[0]["level"] == "writer" and alice[0]["granted_role"] == "reader"


def test_require_access_denial_never_names_groups_but_audits_them(drive, alice):
    drive(_FakeDrive(get={"id": "f1", "name": "ARR", "permissions": [
        {"type": "group", "emailAddress": "finance@everafter.ai", "role": "reader"}]}))
    with pytest.raises(access.DriveAccessDenied) as ei:
        access.require_access("f1", access.READER)
    assert "finance@" not in str(ei.value)
    assert alice[0]["unmapped_groups"] == ("finance@everafter.ai",)
    assert alice[0]["file_id"] == "f1" and alice[0]["name"] == "ARR"


def test_require_access_denies_when_no_requester(monkeypatch, drive, engaged):
    fake = drive(_FakeDrive(get={"id": "f1", "permissions": []}))
    monkeypatch.setattr(access, "resolve_requester", lambda: None)
    audit = []
    monkeypatch.setattr(access, "_audit_denied", lambda **kw: audit.append(kw))
    with pytest.raises(access.DriveAccessDenied) as ei:
        access.require_access("f1", access.READER)
    assert "requesting user" in str(ei.value)
    assert fake.get_calls == []  # no identity → no API call
    assert audit[0]["reason"] == "no_requester"


def test_require_access_denies_on_fetch_error(drive, alice):
    drive(_FakeDrive(get=RuntimeError("boom")))
    with pytest.raises(access.DriveAccessDenied):
        access.require_access("f1", access.READER)
    assert alice[0]["reason"] == "acl_fetch_failed"


def test_audit_denied_writes_record_access(monkeypatch):
    calls = []
    import agent.data_access_audit as daa

    monkeypatch.setattr(daa, "record_access", lambda **kw: calls.append(kw))
    access._audit_denied(
        file_id="f1", name="ARR", level="reader", requester=ALICE,
        granted_role=None, unmapped_groups=("finance@everafter.ai",), reason="denied",
    )
    assert calls[0]["tool"] == "google_drive"
    assert calls[0]["action"] == "drive_access_denied"
    assert "finance@everafter.ai" in calls[0]["target"]
    assert "f1" in calls[0]["target"] and ALICE in calls[0]["target"]


# --------------------------------------------------------------------------- #
# share_with_requester()
# --------------------------------------------------------------------------- #

def test_share_with_requester_creates_writer_permission(drive):
    fake = drive(_FakeDrive())
    r = access.Requester(platform="slack", user_id="U1", email=ALICE)
    assert access.share_with_requester("new1", r) is None
    kw = fake.create_calls[0]
    assert kw["fileId"] == "new1"
    assert kw["body"] == {"type": "user", "role": "writer", "emailAddress": ALICE}
    assert kw["sendNotificationEmail"] is False
    assert kw["supportsAllDrives"] is True


def test_share_with_requester_noop_without_requester(drive):
    fake = drive(_FakeDrive())
    assert access.share_with_requester("new1", None) is None
    assert fake.create_calls == []


def test_share_with_requester_reports_failure(drive, monkeypatch):
    fake = drive(_FakeDrive())

    class _BadP:
        def create(self, **kw):
            return _Req(RuntimeError("quota"))

    monkeypatch.setattr(fake, "permissions", lambda: _BadP())
    r = access.Requester(platform="slack", user_id="U1", email=ALICE)
    msg = access.share_with_requester("new1", r)
    assert msg and ALICE in msg and "quota" in msg
```

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_access.py`
Expected: the Task 1 tests PASS; new ones FAIL with `AttributeError: module ... has no attribute 'Requester'` etc.

- [ ] **Step 3: Append to `plugins/google_drive_sa/access.py`**

```python
# --------------------------------------------------------------------------- #
# Requester
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Requester:
    platform: str
    user_id: str
    email: str


def _engaged() -> bool:
    """Seam: has any session been bound in this process (gateway/cron)?"""
    from gateway.session_context import session_context_engaged

    return session_context_engaged()


def _resolve_email(platform: str, user_id: str) -> Optional[str]:
    from plugins.google_drive_sa.identity import resolve_email

    return resolve_email(platform, user_id)


def _cron_owner() -> Optional[tuple]:
    """``(platform, user_id)`` of the running cron job's owner, else None."""
    try:
        from cron.tool_approval_context import get_cron_job_id

        job_id = get_cron_job_id()
        if not job_id:
            return None
        from agent import automation_ownership as ao

        record = ao.get_record(ao.artifact_key("cron", job_id)) or {}
        owner = record.get("owner") or {}
        platform = str(owner.get("platform") or "").strip()
        user_id = str(owner.get("user_id") or "").strip()
        if platform and user_id:
            return (platform, user_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cron owner lookup failed: %s", exc)
    return None


def is_check_active() -> bool:
    """The check runs only inside a gateway/cron process with it enabled.

    A plain CLI (never engaged the session-context system) is the operator's
    own shell and skips it — the fork's "a shell caller is an admin" rule.
    """
    return load_access_config().enabled and _engaged()


def resolve_requester() -> Optional[Requester]:
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip()
    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    if not (platform and user_id):
        owner = _cron_owner()
        if owner is None:
            return None
        platform, user_id = owner
    email = _resolve_email(platform, user_id)
    if not email:
        return None
    return Requester(platform=platform, user_id=user_id, email=email.lower())


# --------------------------------------------------------------------------- #
# ACL fetch + cache
# --------------------------------------------------------------------------- #

_PERM_FIELDS = "permissions(type,emailAddress,domain,role)"
_GET_FIELDS = f"id,name,driveId,{_PERM_FIELDS}"

_cache_lock = threading.Lock()
# file_id -> (fetched_at, name, acl)
_acl_cache: dict[str, tuple] = {}


def _now() -> float:
    return time.monotonic()


def reset_cache() -> None:
    with _cache_lock:
        _acl_cache.clear()


def _cache_put(file_id: str, name: str, acl: list) -> None:
    with _cache_lock:
        _acl_cache[file_id] = (_now(), name, list(acl))


def _cache_get(file_id: str) -> Optional[tuple]:
    ttl = load_access_config().cache_ttl
    with _cache_lock:
        hit = _acl_cache.get(file_id)
    if hit is None:
        return None
    fetched_at, name, acl = hit
    if _now() - fetched_at > ttl:
        return None
    return name, acl


def _list_permissions(svc: Any, file_id: str) -> list:
    acl: list = []
    token: Optional[str] = None
    while True:
        kw: dict[str, Any] = dict(
            fileId=file_id,
            supportsAllDrives=True,
            pageSize=100,
            fields=f"nextPageToken,{_PERM_FIELDS}",
        )
        if token:
            kw["pageToken"] = token
        resp = svc.permissions().list(**kw).execute() or {}
        acl.extend(resp.get("permissions") or [])
        token = resp.get("nextPageToken")
        if not token:
            return acl


def fetch_acl(file_id: str) -> tuple:
    """``(name, acl)`` for *file_id*, cached. Raises on any API failure.

    ``files.get`` returns the ACL inline for My Drive files the SA can share;
    shared-drive items (and files the SA can only read) come back without
    ``permissions`` and need ``permissions.list`` — which also includes the
    drive-level memberships shared-drive access is usually granted through.
    """
    cached = _cache_get(file_id)
    if cached is not None:
        return cached
    from plugins.google_drive_sa import client

    svc = client.get_service()
    meta = svc.files().get(fileId=file_id, fields=_GET_FIELDS, supportsAllDrives=True).execute() or {}
    name = str(meta.get("name") or "")
    acl = meta.get("permissions")
    if acl is None:
        acl = _list_permissions(svc, file_id)
    _cache_put(file_id, name, acl)
    return name, list(acl)


# --------------------------------------------------------------------------- #
# require_access
# --------------------------------------------------------------------------- #

class DriveAccessDenied(Exception):
    """Raised by require_access; ``str(exc)`` is safe to show the model."""


def _audit_denied(
    *, file_id: str, name: str, level: str, requester: str,
    granted_role: Optional[str], unmapped_groups: tuple, reason: str,
) -> None:
    """Audit-log a denial with the detail the tool result deliberately omits."""
    try:
        from agent.data_access_audit import record_access

        record_access(
            tool="google_drive",
            action="drive_access_denied",
            target=(
                f"drive:{file_id} name={name!r} level={level} requester={requester or '-'} "
                f"granted={granted_role or '-'} reason={reason} "
                f"unmapped_groups={','.join(unmapped_groups) or '-'}"
            ),
        )
    except Exception:  # pragma: no cover - auditing never breaks a tool
        pass


def _denial_text(email: str, level: str) -> str:
    verb = "edit" if level == WRITER else "access"
    return (
        f"Access denied: {email} does not have permission to {verb} this file. "
        "Ask the file's owner to share it with you (or with a group the "
        "operator has mapped), then try again."
    )


def require_access(file_id: str, level: str) -> Optional[Requester]:
    """Gate one tool call. Returns the requester, or None if the check is off.

    Raises :class:`DriveAccessDenied` when the requester cannot be resolved,
    the ACL cannot be fetched, or the ACL does not grant *level*.
    """
    if not is_check_active():
        return None
    requester = resolve_requester()
    if requester is None:
        _audit_denied(
            file_id=file_id, name="", level=level, requester="",
            granted_role=None, unmapped_groups=(), reason="no_requester",
        )
        raise DriveAccessDenied(
            "Access denied: the requesting user could not be identified, so "
            "Drive access cannot be verified. (No platform identity in this "
            "session, or no email is mapped for it — an operator can set "
            "slack.user_emails.)"
        )
    try:
        name, acl = fetch_acl(file_id)
    except Exception as exc:
        logger.warning("ACL fetch for %s failed (%s); denying", file_id, exc)
        _audit_denied(
            file_id=file_id, name="", level=level, requester=requester.email,
            granted_role=None, unmapped_groups=(), reason="acl_fetch_failed",
        )
        raise DriveAccessDenied(
            f"Access denied: could not verify {requester.email}'s access to this "
            f"file ({type(exc).__name__}). Check the file ID, or that the file "
            "is shared with the service account."
        ) from exc
    decision = evaluate(acl, requester.email, load_access_config())
    if decision.satisfies(level):
        return requester
    _audit_denied(
        file_id=file_id, name=name, level=level, requester=requester.email,
        granted_role=decision.granted_role, unmapped_groups=decision.unmapped_groups,
        reason="denied",
    )
    raise DriveAccessDenied(_denial_text(requester.email, level))


# --------------------------------------------------------------------------- #
# share_with_requester
# --------------------------------------------------------------------------- #

def share_with_requester(file_id: str, requester: Optional[Requester]) -> Optional[str]:
    """Add *requester* as writer on a file the SA just created in its own root.

    Without this, nobody but the SA is on the new file's ACL and the person
    who asked for it could not read it back. Returns an error string on
    failure (the file exists; the caller reports it), None on success/no-op.
    """
    if requester is None:
        return None
    try:
        from plugins.google_drive_sa import client

        client.get_service().permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "writer", "emailAddress": requester.email},
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ).execute()
        with _cache_lock:
            _acl_cache.pop(file_id, None)
        return None
    except Exception as exc:
        logger.warning("could not share %s with %s: %s", file_id, requester.email, exc)
        return (
            f"File created, but sharing it with {requester.email} failed "
            f"({type(exc).__name__}: {exc}); they may not be able to open it."
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_access.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/google_drive_sa/access.py tests/plugins/test_google_drive_sa_access.py
git commit -m "feat(gdrive): requester resolution, cached ACL fetch, require_access, share-on-create

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Gate the read/write/create handlers

**Files:**
- Modify: `plugins/google_drive_sa/tools.py` (`create_drive_file`, `_handle_drive_read_file`, `_handle_drive_upload`, `_handle_drive_create_folder`, schema text)
- Modify: `plugins/google_drive_sa/sheets_tools.py` (all five handlers)
- Modify: `plugins/google_drive_sa/docs_tools.py` (all four handlers)
- Modify: `tests/plugins/test_google_drive_sa_plugin.py` (autouse fixture)
- Test: `tests/plugins/test_google_drive_sa_gated_handlers.py`

**Interfaces:**
- Consumes: `access.require_access`, `access.DriveAccessDenied`, `access.share_with_requester`, `access.READER/WRITER` (Task 5).
- Produces: `create_drive_file(name, mime_type, folder_id="", fields=..., requester=None) -> dict` — when `folder_id` is empty and `requester` is given, shares the new file and puts any share error under `result["share_warning"]`.

- [ ] **Step 1: Keep the pre-existing plugin tests deterministic**

Add to `tests/plugins/test_google_drive_sa_plugin.py`, right after the `stub_googleapiclient_http` fixture:

```python
@pytest.fixture(autouse=True)
def _access_check_off(monkeypatch):
    """These tests cover the Drive API plumbing, not the per-user gate
    (tests/plugins/test_google_drive_sa_gated_handlers.py does). Force the
    gate off so a process that happens to have engaged session context
    doesn't turn every call into a denial."""
    from plugins.google_drive_sa import access

    monkeypatch.setattr(access, "is_check_active", lambda: False)
```

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_plugin.py` — Expected: PASS (nothing changed yet; this just proves the fixture is harmless).

- [ ] **Step 2: Write the failing handler tests**

```python
# tests/plugins/test_google_drive_sa_gated_handlers.py
"""Every Drive/Sheets/Docs handler must call the per-user access gate."""

from __future__ import annotations

import json
import sys
import types

import pytest

from plugins.google_drive_sa import access
from plugins.google_drive_sa import docs_tools as dc
from plugins.google_drive_sa import sheets_tools as sh
from plugins.google_drive_sa import tools as gd

ALICE = "alice@everafter.ai"


@pytest.fixture(autouse=True)
def stub_googleapiclient_http(monkeypatch):
    try:  # pragma: no cover - depends on env
        import googleapiclient.http  # noqa: F401
        return
    except Exception:
        pass
    pkg = sys.modules.get("googleapiclient") or types.ModuleType("googleapiclient")
    http_mod = types.ModuleType("googleapiclient.http")

    class _MediaInMemoryUpload:
        def __init__(self, body, mimetype="application/octet-stream", resumable=False):
            self.body = body

    http_mod.MediaInMemoryUpload = _MediaInMemoryUpload
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http_mod)


class _Req:
    def __init__(self, r):
        self._r = r

    def execute(self):
        return self._r


class _Files:
    def __init__(self):
        self.calls = []

    def get(self, **kw):
        self.calls.append(("get", kw))
        return _Req({"id": kw["fileId"], "name": "n", "mimeType": "text/plain"})

    def get_media(self, **kw):
        self.calls.append(("get_media", kw))
        return _Req(b"secret")

    def export_media(self, **kw):
        self.calls.append(("export_media", kw))
        return _Req(b"secret")

    def create(self, **kw):
        self.calls.append(("create", kw))
        return _Req({"id": "new1", "name": kw["body"].get("name")})

    def update(self, **kw):
        self.calls.append(("update", kw))
        return _Req({"id": kw["fileId"]})


class _Perms:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return _Req({"id": "p1"})


class _Drive:
    def __init__(self):
        self._files = _Files()
        self._perms = _Perms()

    def files(self):
        return self._files

    def permissions(self):
        return self._perms


class _Values:
    def __init__(self):
        self.calls = []

    def _rec(self, name, kw, result):
        self.calls.append((name, kw))
        return _Req(result)

    def get(self, **kw):
        return self._rec("get", kw, {"range": kw["range"], "values": [["x"]]})

    def update(self, **kw):
        return self._rec("update", kw, {"updatedRange": kw["range"], "updatedCells": 1})

    def append(self, **kw):
        return self._rec("append", kw, {"updates": {"updatedRange": "S!A2", "updatedRows": 1}})

    def clear(self, **kw):
        return self._rec("clear", kw, {"clearedRange": kw["range"]})


class _Sheets:
    def __init__(self):
        self._v = _Values()

    def spreadsheets(self):
        s = self

        class _S:
            def values(self_inner):
                return s._v

        return _S()


class _Docs:
    def __init__(self):
        self.calls = []

    def documents(self):
        d = self

        class _D:
            def get(self_inner, **kw):
                d.calls.append(("get", kw))
                return _Req({"title": "T", "body": {"content": [
                    {"endIndex": 6, "paragraph": {"elements": [{"textRun": {"content": "hi\n"}}]}}]}})

            def batchUpdate(self_inner, **kw):
                d.calls.append(("batchUpdate", kw))
                return _Req({"replies": [{"replaceAllText": {"occurrencesChanged": 1}}]})

        return _D()


@pytest.fixture
def services(monkeypatch):
    from plugins.google_drive_sa import client as gd_client

    drive, sheets, docs = _Drive(), _Sheets(), _Docs()
    monkeypatch.setattr(gd_client, "get_service", lambda: drive)
    monkeypatch.setattr(gd_client, "get_sheets_service", lambda: sheets)
    monkeypatch.setattr(gd_client, "get_docs_service", lambda: docs)
    return drive, sheets, docs


@pytest.fixture
def gate(monkeypatch):
    """Record require_access calls; deny when ``deny`` is set."""
    state = {"calls": [], "deny": False}

    def _require(file_id, level):
        state["calls"].append((file_id, level))
        if state["deny"]:
            raise access.DriveAccessDenied("Access denied: nope")
        return access.Requester(platform="slack", user_id="U1", email=ALICE)

    monkeypatch.setattr(access, "require_access", _require)
    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(
        access, "resolve_requester",
        lambda: access.Requester(platform="slack", user_id="U1", email=ALICE),
    )
    return state


READ_CASES = [
    ("drive_read_file", lambda: gd._handle_drive_read_file({"file_id": "F"}), "F"),
    ("sheets_get_values", lambda: sh._handle_sheets_get_values({"spreadsheet_id": "S", "range": "A1"}), "S"),
    ("docs_get", lambda: dc._handle_docs_get({"document_id": "D"}), "D"),
]

WRITE_CASES = [
    ("sheets_update_values", lambda: sh._handle_sheets_update_values({"spreadsheet_id": "S", "range": "A1", "values": [["1"]]}), "S"),
    ("sheets_append_values", lambda: sh._handle_sheets_append_values({"spreadsheet_id": "S", "range": "A1", "values": [["1"]]}), "S"),
    ("sheets_clear", lambda: sh._handle_sheets_clear({"spreadsheet_id": "S", "range": "A1"}), "S"),
    ("docs_insert_text", lambda: dc._handle_docs_insert_text({"document_id": "D", "text": "x"}), "D"),
    ("docs_replace_text", lambda: dc._handle_docs_replace_text({"document_id": "D", "find": "a"}), "D"),
    ("drive_upload_update", lambda: gd._handle_drive_upload({"file_id": "F", "content": "x"}), "F"),
]

PARENT_CASES = [
    ("drive_upload_new", lambda: gd._handle_drive_upload({"name": "n", "content": "x", "folder_id": "P"}), "P"),
    ("drive_create_folder", lambda: gd._handle_drive_create_folder({"name": "n", "parent_id": "P"}), "P"),
    ("sheets_create", lambda: sh._handle_sheets_create({"title": "t", "folder_id": "P"}), "P"),
    ("docs_create", lambda: dc._handle_docs_create({"title": "t", "folder_id": "P"}), "P"),
]


@pytest.mark.parametrize("name,call,target", READ_CASES, ids=[c[0] for c in READ_CASES])
def test_read_tools_require_reader(services, gate, name, call, target):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == [(target, access.READER)]


@pytest.mark.parametrize("name,call,target", WRITE_CASES, ids=[c[0] for c in WRITE_CASES])
def test_write_tools_require_writer(services, gate, name, call, target):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == [(target, access.WRITER)]


@pytest.mark.parametrize("name,call,target", PARENT_CASES, ids=[c[0] for c in PARENT_CASES])
def test_create_in_folder_requires_writer_on_parent(services, gate, name, call, target):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == [(target, access.WRITER)]
    # In a folder: inheritance does the sharing; no permissions.create.
    assert services[0].permissions().created == []


@pytest.mark.parametrize(
    "name,call,target",
    READ_CASES + WRITE_CASES + PARENT_CASES,
    ids=[c[0] for c in READ_CASES + WRITE_CASES + PARENT_CASES],
)
def test_denied_tools_make_no_api_call(services, gate, name, call, target):
    gate["deny"] = True
    out = json.loads(call())
    assert out.get("success") is not True
    assert "Access denied" in out.get("error", "")
    drive, sheets, docs = services
    assert drive.files().calls == []
    assert sheets.spreadsheets().values().calls == []
    assert docs.calls == []


CREATE_ROOT_CASES = [
    ("drive_upload_new", lambda: gd._handle_drive_upload({"name": "n", "content": "x"})),
    ("drive_create_folder", lambda: gd._handle_drive_create_folder({"name": "n"})),
    ("sheets_create", lambda: sh._handle_sheets_create({"title": "t"})),
    ("docs_create", lambda: dc._handle_docs_create({"title": "t"})),
]


@pytest.mark.parametrize("name,call", CREATE_ROOT_CASES, ids=[c[0] for c in CREATE_ROOT_CASES])
def test_create_in_root_shares_with_requester(services, gate, name, call):
    out = json.loads(call())
    assert out.get("success") is True
    assert gate["calls"] == []  # nothing to pre-check
    created = services[0].permissions().created
    assert len(created) == 1
    assert created[0]["fileId"] == "new1"
    assert created[0]["body"]["emailAddress"] == ALICE
    assert created[0]["body"]["role"] == "writer"


def test_create_in_root_reports_share_failure(services, gate, monkeypatch):
    monkeypatch.setattr(access, "share_with_requester", lambda fid, r: "File created, but sharing failed")
    out = json.loads(sh._handle_sheets_create({"title": "t"}))
    assert out["success"] is True
    assert "sharing failed" in out["share_warning"]


def test_create_in_root_does_not_share_when_check_inactive(services, monkeypatch):
    monkeypatch.setattr(access, "is_check_active", lambda: False)
    out = json.loads(dc._handle_docs_create({"title": "t"}))
    assert out["success"] is True
    assert services[0].permissions().created == []
```

- [ ] **Step 3: Run to verify they fail**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_gated_handlers.py`
Expected: gate-related assertions FAIL (`gate["calls"] == []`, no `share_warning`, etc.).

- [ ] **Step 4: Implement in `tools.py`**

Add `from plugins.google_drive_sa import access` to the imports. Then:

`create_drive_file`:

```python
def create_drive_file(
    name: str,
    mime_type: str,
    folder_id: str = "",
    fields: str = "id, name, mimeType, parents, webViewLink",
    requester: "access.Requester | None" = None,
) -> dict:
    """Create an (empty) Drive file of *mime_type*, optionally in *folder_id*.

    Used by drive_create_folder and the Docs/Sheets ``*_create`` tools — the
    Drive API is the only create path that can drop a new Google-native file
    straight into a *shared* folder (the Docs/Sheets ``create`` endpoints
    always land in the SA's own My Drive root).

    Without a folder the file lands in the SA's root with only the SA on its
    ACL, so *requester* (when the access check is on) is added as writer;
    a share failure is reported under ``share_warning``, not raised.
    """
    body: dict[str, Any] = {"name": name, "mimeType": mime_type}
    if folder_id:
        body["parents"] = [folder_id]
    result = (
        client.get_service()
        .files()
        .create(body=body, fields=fields, supportsAllDrives=True)
        .execute()
    )
    if not folder_id and requester is not None:
        warning = access.share_with_requester(str(result.get("id") or ""), requester)
        if warning:
            result["share_warning"] = warning
    return result


def _requester_for_create(folder_id: str) -> "access.Requester | None":
    """Gate a create: writer on the parent when given, else the requester to share with."""
    if folder_id:
        return access.require_access(folder_id, access.WRITER)
    if not access.is_check_active():
        return None
    return access.resolve_requester()
```

`_handle_drive_read_file` — insert right after the `file_id` validation, before `try`:

```python
    try:
        access.require_access(file_id, access.READER)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
```

`_handle_drive_upload` — insert after `mime_type = ...` and before `try:`:

```python
    folder_id = _str(args, "folder_id")
    try:
        requester = (
            access.require_access(file_id, access.WRITER)
            if file_id
            else _requester_for_create(folder_id)
        )
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
```

and in the create branch replace `if args.get("folder_id"): body["parents"] = [_str(args, "folder_id")]` with `if folder_id: body["parents"] = [folder_id]`, and after `result = (... .create(...).execute())` add:

```python
            if not folder_id and requester is not None:
                warning = access.share_with_requester(str(result.get("id") or ""), requester)
                if warning:
                    result["share_warning"] = warning
```

Update `DRIVE_UPLOAD_SCHEMA["parameters"]["properties"]["folder_id"]["description"]` to `"Parent folder ID for a new file (you need edit access to it)."`.

`_handle_drive_create_folder`:

```python
def _handle_drive_create_folder(args: dict, **_: Any) -> str:
    name = _str(args, "name")
    if not name:
        return tool_error("name is required")
    parent_id = _str(args, "parent_id")
    try:
        requester = _requester_for_create(parent_id)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
    try:
        result = create_drive_file(
            name,
            "application/vnd.google-apps.folder",
            parent_id,
            fields="id, name, parents, webViewLink",
            requester=requester,
        )
        out: dict[str, Any] = {"success": True, "folder": result}
        if result.get("share_warning"):
            out["share_warning"] = result.pop("share_warning")
        return tool_result(out)
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)
```

- [ ] **Step 5: Implement in `sheets_tools.py`**

Change the import line to `from plugins.google_drive_sa.tools import _drive_error, _requester_for_create, _str, create_drive_file` and add `from plugins.google_drive_sa import access`.

In `_handle_sheets_get_values`, after the `if not sid or not rng:` check:

```python
    try:
        access.require_access(sid, access.READER)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
```

In `_handle_sheets_update_values`, `_handle_sheets_append_values`, `_handle_sheets_clear`, after the same `if not sid or not rng:` check (before the `_coerce_rows` try where present):

```python
    try:
        access.require_access(sid, access.WRITER)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
```

`_handle_sheets_create`:

```python
def _handle_sheets_create(args: dict, **_: Any) -> str:
    title = _str(args, "title")
    if not title:
        return tool_error("title is required")
    folder_id = _str(args, "folder_id")
    try:
        requester = _requester_for_create(folder_id)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
    try:
        result = create_drive_file(
            title, "application/vnd.google-apps.spreadsheet", folder_id, requester=requester
        )
        out: dict[str, Any] = {"success": True, "spreadsheet": result}
        if result.get("share_warning"):
            out["share_warning"] = result.pop("share_warning")
        return tool_result(out)
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)
```

Update the `sheets_create` / `sheets_update_values` schema descriptions: replace "the SA must have Editor access on that folder" with "you need edit access to that folder"; "Needs Editor access." → "You need edit access to the sheet."

- [ ] **Step 6: Implement in `docs_tools.py`**

Change the import line to `from plugins.google_drive_sa.tools import _drive_error, _requester_for_create, _str, create_drive_file` and add `from plugins.google_drive_sa import access`.

In `_handle_docs_get`, right after `if not doc_id: return tool_error(...)`:

```python
    try:
        access.require_access(doc_id, access.READER)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
```

In `_handle_docs_insert_text` (after the `text is None or text == ""` check) and `_handle_docs_replace_text` (after the `if not doc_id or not find` check):

```python
    try:
        access.require_access(doc_id, access.WRITER)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
```

`_handle_docs_create`:

```python
def _handle_docs_create(args: dict, **_: Any) -> str:
    title = _str(args, "title")
    if not title:
        return tool_error("title is required")
    folder_id = _str(args, "folder_id")
    try:
        requester = _requester_for_create(folder_id)
    except access.DriveAccessDenied as exc:
        return tool_error(str(exc))
    try:
        result = create_drive_file(
            title, "application/vnd.google-apps.document", folder_id, requester=requester
        )
        out: dict[str, Any] = {"success": True, "document": result}
        if result.get("share_warning"):
            out["share_warning"] = result.pop("share_warning")
        return tool_result(out)
    except Exception as exc:  # noqa: BLE001
        return _drive_error(exc)
```

Update the `docs_create` schema description: "the SA must have Editor access on that folder" → "you need edit access to that folder".

- [ ] **Step 7: Run to verify**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_gated_handlers.py tests/plugins/test_google_drive_sa_plugin.py tests/plugins/test_google_drive_sa_access.py`
Expected: all PASS (the pre-existing plugin tests still pass thanks to the autouse fixture).

- [ ] **Step 8: Commit**

```bash
git add plugins/google_drive_sa/tools.py plugins/google_drive_sa/sheets_tools.py plugins/google_drive_sa/docs_tools.py tests/plugins/test_google_drive_sa_gated_handlers.py tests/plugins/test_google_drive_sa_plugin.py
git commit -m "feat(gdrive): gate every Drive/Sheets/Docs handler on the requesting user's ACL

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Listing filter

**Files:**
- Modify: `plugins/google_drive_sa/access.py` (append `filter_listing`)
- Modify: `plugins/google_drive_sa/tools.py` (`_LIST_FIELDS`, `_handle_drive_list_files`, `DRIVE_LIST_SCHEMA` description)
- Test: `tests/plugins/test_google_drive_sa_gated_handlers.py` (append)

**Interfaces:**
- Produces: `access.filter_listing(files: list[dict], level: str = READER) -> list[dict]` — returns only files the current requester can access, with the `permissions` key removed from each; returns `files` unchanged (minus `permissions`) when the check is inactive; returns `[]` when active but no requester.

- [ ] **Step 1: Write the failing tests**

Append to `tests/plugins/test_google_drive_sa_gated_handlers.py`:

```python
# --------------------------------------------------------------------------- #
# drive_list_files
# --------------------------------------------------------------------------- #

class _ListDrive(_Drive):
    def __init__(self, files, perms_by_id=None):
        super().__init__()
        self._list_files = files
        self._perms_by_id = perms_by_id or {}
        self.list_kw = None
        self.perm_list_calls = []

    def files(self):
        outer = self

        class _F(_Files):
            def list(self_inner, **kw):
                outer.list_kw = kw
                return _Req({"files": outer._list_files})

        f = _F()
        f.calls = self._files.calls
        return f

    def permissions(self):
        outer = self

        class _P(_Perms):
            def list(self_inner, **kw):
                outer.perm_list_calls.append(kw["fileId"])
                return _Req({"permissions": outer._perms_by_id.get(kw["fileId"], [])})

        p = _P()
        p.created = self._perms.created
        return p


@pytest.fixture
def listing(monkeypatch):
    from plugins.google_drive_sa import client as gd_client

    def _install(files, perms_by_id=None):
        d = _ListDrive(files, perms_by_id)
        monkeypatch.setattr(gd_client, "get_service", lambda: d)
        access.reset_cache()
        return d

    return _install


@pytest.fixture
def alice_active(monkeypatch):
    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(
        access, "resolve_requester",
        lambda: access.Requester(platform="slack", user_id="U1", email=ALICE),
    )
    monkeypatch.setattr(
        access, "load_access_config",
        lambda: access.AccessConfig(everyone_groups=frozenset({"all@everafter.ai"})),
    )


def test_list_requests_permissions_inline(listing, alice_active):
    d = listing([])
    gd._handle_drive_list_files({})
    assert "permissions(type,emailAddress,domain,role)" in d.list_kw["fields"]
    assert "driveId" in d.list_kw["fields"]


def test_list_filters_by_inline_acl_and_strips_permissions(listing, alice_active):
    d = listing([
        {"id": "ok", "name": "Mine", "permissions": [{"type": "user", "emailAddress": ALICE, "role": "reader"}]},
        {"id": "no", "name": "ARR", "permissions": [{"type": "group", "emailAddress": "finance@everafter.ai", "role": "reader"}]},
        {"id": "all", "name": "Handbook", "permissions": [{"type": "group", "emailAddress": "all@everafter.ai", "role": "reader"}]},
    ])
    out = json.loads(gd._handle_drive_list_files({}))
    assert [f["id"] for f in out["files"]] == ["ok", "all"]
    assert out["count"] == 2
    assert all("permissions" not in f for f in out["files"])
    assert "hidden" not in json.dumps(out)
    assert d.perm_list_calls == []


def test_list_fetches_acl_for_shared_drive_items(listing, alice_active):
    d = listing(
        [
            {"id": "sd1", "name": "A", "driveId": "D"},
            {"id": "sd2", "name": "B", "driveId": "D"},
        ],
        perms_by_id={
            "sd1": [{"type": "domain", "domain": "everafter.ai", "role": "reader"}],
            "sd2": [],
        },
    )
    out = json.loads(gd._handle_drive_list_files({}))
    assert [f["id"] for f in out["files"]] == ["sd1"]
    assert sorted(d.perm_list_calls) == ["sd1", "sd2"]


def test_list_drops_items_whose_acl_fetch_fails(listing, alice_active, monkeypatch):
    listing([{"id": "sd1", "name": "A", "driveId": "D"}])

    def _boom(file_id):
        raise RuntimeError("403")

    monkeypatch.setattr(access, "fetch_acl", _boom)
    out = json.loads(gd._handle_drive_list_files({}))
    assert out["files"] == []


def test_list_returns_nothing_when_active_but_no_requester(listing, monkeypatch):
    listing([{"id": "x", "name": "X", "permissions": [{"type": "anyone", "role": "reader"}]}])
    monkeypatch.setattr(access, "is_check_active", lambda: True)
    monkeypatch.setattr(access, "resolve_requester", lambda: None)
    out = json.loads(gd._handle_drive_list_files({}))
    assert out["files"] == []


def test_list_unfiltered_when_check_inactive(listing, monkeypatch):
    listing([{"id": "x", "name": "X", "permissions": [{"type": "user", "emailAddress": "z@z", "role": "reader"}]}])
    monkeypatch.setattr(access, "is_check_active", lambda: False)
    out = json.loads(gd._handle_drive_list_files({}))
    assert [f["id"] for f in out["files"]] == ["x"]
    assert "permissions" not in out["files"][0]


def test_list_primes_cache_for_following_read(listing, alice_active, monkeypatch):
    d = listing([{"id": "ok", "name": "Mine", "permissions": [{"type": "user", "emailAddress": ALICE, "role": "reader"}]}])
    gd._handle_drive_list_files({})
    name, acl = access.fetch_acl("ok")
    assert name == "Mine" and acl[0]["emailAddress"] == ALICE
    assert d.files().calls == []  # served from cache, no files.get
```

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_gated_handlers.py -k list`
Expected: FAIL (`permissions` not in fields; nothing filtered).

- [ ] **Step 3: Append to `access.py`**

```python
# --------------------------------------------------------------------------- #
# Listing filter
# --------------------------------------------------------------------------- #

_LIST_POOL_WORKERS = 8


def _strip_acl(files: list) -> list:
    out = []
    for f in files:
        if isinstance(f, dict):
            f = {k: v for k, v in f.items() if k != "permissions"}
        out.append(f)
    return out


def filter_listing(files: list, level: str = READER) -> list:
    """Drop listed files the requester cannot access at *level*.

    Files that came back with an inline ``permissions`` array are evaluated
    in place (and prime the ACL cache). The rest — shared-drive items, or
    files the SA can't share — are fetched in a small thread pool. A file
    whose ACL cannot be fetched is dropped (fail closed). No count of hidden
    items is exposed anywhere.
    """
    if not is_check_active():
        return _strip_acl(files)
    requester = resolve_requester()
    if requester is None:
        return []
    cfg = load_access_config()

    def _decide(f: dict) -> bool:
        file_id = str(f.get("id") or "")
        if not file_id:
            return False
        acl = f.get("permissions")
        if acl is None:
            try:
                _, acl = fetch_acl(file_id)
            except Exception as exc:
                logger.debug("listing: ACL fetch for %s failed (%s); hiding", file_id, exc)
                return False
        else:
            _cache_put(file_id, str(f.get("name") or ""), acl)
        return evaluate(acl, requester.email, cfg).satisfies(level)

    inline = [f for f in files if isinstance(f, dict) and f.get("permissions") is not None]
    remote = [f for f in files if isinstance(f, dict) and f.get("permissions") is None]
    allowed: set = set()
    for f in inline:
        if _decide(f):
            allowed.add(id(f))
    if remote:
        with ThreadPoolExecutor(max_workers=min(_LIST_POOL_WORKERS, len(remote))) as pool:
            for f, ok in zip(remote, pool.map(_decide, remote)):
                if ok:
                    allowed.add(id(f))
    return _strip_acl([f for f in files if id(f) in allowed])
```

- [ ] **Step 4: Wire into `tools.py`**

```python
_LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, modifiedTime, size, parents, webViewLink, "
    "driveId, permissions(type,emailAddress,domain,role))"
)
```

In `_handle_drive_list_files`, replace `files = resp.get("files", [])` with:

```python
        files = access.filter_listing(resp.get("files", []), access.READER)
```

Change `DRIVE_LIST_SCHEMA["description"]` to:

```python
    "description": (
        "List or search Google Drive files/folders you have access to. "
        "Combine filters, or pass a raw Drive `query`."
    ),
```

- [ ] **Step 5: Run to verify**

Run: `scripts/run_tests.sh tests/plugins/test_google_drive_sa_gated_handlers.py tests/plugins/test_google_drive_sa_plugin.py`
Expected: all PASS. (`test_list_builds_query_from_filters` in the old file still expects `count == 1` — with the check forced off, `filter_listing` returns the single fake file unchanged.)

- [ ] **Step 6: Commit**

```bash
git add plugins/google_drive_sa/access.py plugins/google_drive_sa/tools.py tests/plugins/test_google_drive_sa_gated_handlers.py
git commit -m "feat(gdrive): drive_list_files hides files the requester cannot access

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Fork inventory guard + docs

**Files:**
- Modify: `tests/test_fork_feature_inventory.py`
- Modify: `CLAUDE.md` (fork section, after the "Fork-added toolsets" bullets)
- Modify: `plugins/google_drive_sa/client.py:13-15` and `plugins/google_drive_sa/__init__.py:5-7` (docstring sentences about sharing)

- [ ] **Step 1: Add the guards**

In `WIRING`, after the `("ownership tool", ...)` line:

```python
    # ── Drive per-user access check ───────────────────────────────────────
    ("Drive access check", "plugins/google_drive_sa/access.py", "def require_access", "gate"),
    ("Drive access check", "plugins/google_drive_sa/access.py", "def filter_listing", "listing filter"),
    ("Drive access check", "plugins/google_drive_sa/tools.py", "access.require_access(file_id, access.READER)", "read gated"),
    ("Drive access check", "plugins/google_drive_sa/tools.py", "access.filter_listing(", "list gated"),
    ("Drive access check", "plugins/google_drive_sa/sheets_tools.py", "access.require_access(sid, access.WRITER)", "sheet writes gated"),
    ("Drive access check", "plugins/google_drive_sa/docs_tools.py", "access.require_access(doc_id, access.WRITER)", "doc writes gated"),
    ("Drive access check", "plugins/google_drive_sa/identity.py", "def resolve_email", "email resolver"),
    ("Drive access check", "cron/tool_approval_context.py", "def get_cron_job_id", "cron owner path"),
    ("Drive access check", "cron/scheduler.py", "job_id=job.get(\"id\")", "scheduler passes job id"),
    ("Config bridge", "gateway/config.py", 'bridged["user_emails"]', "user_emails -> extra"),
```

Run: `scripts/run_tests.sh tests/test_fork_feature_inventory.py` — Expected: PASS. If any needle fails, the implementation drifted from this plan; fix the needle to the real call-site text (not the code).

- [ ] **Step 2: Docs**

In `CLAUDE.md`, add a bullet under "Fork-added toolsets" (after `video_frames`):

```markdown
- **Google Drive per-user access check** — [plugins/google_drive_sa/access.py](plugins/google_drive_sa/access.py).
  The Drive/Sheets/Docs plugin acts as ONE service account, so "shared with the
  SA" would otherwise be "readable by every user whose role grants the toolset".
  Every handler now calls `require_access(file_id, reader|writer)` before its API
  call: resolve the requester (session contextvars, or the cron job's owner via
  the ownership registry — `cron/tool_approval_context.get_cron_job_id`), fetch
  the file's ACL as the SA (cached, `google_drive.acl_cache_ttl_seconds`), and
  evaluate it. `drive_list_files` filters results the same way (inline
  `permissions` from `files.list`; `permissions.list` in a thread pool for
  shared-drive items). **Groups fail closed** — no DWD, so a group grant counts
  only via `google_drive.everyone_groups` (treated as domain-wide) or
  `google_drive.group_members` (manual map); denials audit the unmapped groups to
  `audit/data-access.log` (`drive_access_denied`) so you can see what to map.
  Email comes from `slack.user_emails` → Slack `users.info` (needs the
  `users:read.email` scope) → written back into `slack.user_emails`
  (`hermes users add/update --email`). Files created without a folder are shared
  with the requester as writer. No requester in a gateway/cron session → deny;
  local CLI (session context never engaged) → skipped. Kill switch
  `google_drive.access_check: false`. Design:
  [docs/superpowers/specs/2026-09-14-drive-per-user-access-check-design.md](docs/superpowers/specs/2026-09-14-drive-per-user-access-check-design.md).
```

In `plugins/google_drive_sa/client.py` docstring, change the "The agent acts as the *service account itself*" paragraph to end with: `Per-user access is enforced on top by :mod:\`plugins.google_drive_sa.access\` — the SA's share is necessary, the requesting user's own ACL entry is what actually grants a tool call.` Make the equivalent one-sentence addition to the `__init__.py` module docstring.

- [ ] **Step 3: Full-package verification**

Run, one after the other (never in parallel):

```bash
scripts/run_tests.sh tests/plugins/
scripts/run_tests.sh tests/cron/
scripts/run_tests.sh tests/cli/
scripts/run_tests.sh tests/test_fork_feature_inventory.py
scripts/run_tests.sh tests/gateway/test_config.py
```

Expected: all PASS. (Note `tests/tools/` has ~22 known environment-dependent failures — not touched by this plan; do not run it to "check".)

- [ ] **Step 4: Commit**

```bash
git add tests/test_fork_feature_inventory.py CLAUDE.md plugins/google_drive_sa/client.py plugins/google_drive_sa/__init__.py
git commit -m "docs(gdrive): document the per-user access check; guard its wiring in the fork inventory

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Deployment checklist (after merge — operator steps, not code)

1. Slack app: add the `users:read.email` bot scope, reinstall.
2. `~/.hermes/config.yaml` on the VM: add the `google_drive:` block with `everyone_groups` BEFORE restarting, or every "shared with all@" file goes dark.
3. `hermes own list` — give every cron job that reads Drive an owner (`hermes own claim cron:<id>`), or they start denying.
4. Restart the gateway; tail `~/.hermes/audit/data-access.log` for `drive_access_denied` lines and map recurring `unmapped_groups` into `google_drive.group_members`.
