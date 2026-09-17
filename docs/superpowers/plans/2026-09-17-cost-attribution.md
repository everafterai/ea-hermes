# Cost Attribution (`hermes costs`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `hermes costs` CLI that attributes Hermes spend to Slack channels and cron jobs over time, plus a `pricing.overrides` config so models missing from the catalog (the VM's `gpt-5.4-mini`) are priced, and a `--reprice` pass that prices stored history.

**Architecture:** A new pure query module `agent/cost_attribution.py` walks every session in `state.db` up its `parent_session_id` chain to a root, derives the job key (from the `cron_<job>_<ts>` session id) and channel key(s) (from `origin_json`, or the cron job's delivery targets) at query time, and aggregates into views. No schema change, no write-path edits. A thin parser/formatter module `hermes_cli/subcommands/costs.py` renders table/JSON/CSV. `agent/usage_pricing.get_pricing_entry` gains an override lookup read from the `pricing:` config block.

**Tech Stack:** Python 3, sqlite3 (recursive CTE), argparse, `decimal.Decimal`, existing `SessionDB` (`hermes_state.py`), existing pricing helpers (`agent/usage_pricing.py`), pytest via `scripts/run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-17-cost-attribution-design.md`

## Global Constraints

- Run tests only through `scripts/run_tests.sh <path>`; never bare pytest; never the whole suite.
- Tests must not write to `~/.hermes/` (the autouse fixture redirects `HERMES_HOME`; `SessionDB(db_path=tmp_path / "x.db")` for stores).
- Never hardcode `~/.hermes`; config is read via `hermes_cli.config.read_raw_config()`.
- New code lives in **fork-only files** wherever possible; the only upstream file edited for logic is `agent/usage_pricing.py` (one hook at the top of `get_pricing_entry`) and `hermes_cli/main.py` (registration only).
- Cost precedence everywhere is `COALESCE(actual_cost_usd, estimated_cost_usd)`, the same as `SessionDB.usage_totals`.
- Sub-cent money renders through `agent.usage_pricing.format_cost_label(Decimal)`.
- `--reprice` only touches rows whose `cost_status` is NULL or `"unknown"`; priced rows are never rewritten. No force flag.
- Commit after every task with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Regex for cron roots: `^cron_(?P<job>.+)_\d{8}_\d{6}$`.
- Buckets are UTC; week starts Monday (ISO week label `YYYY-Www`).

---

## File map

| File | Responsibility |
|---|---|
| `agent/cost_attribution.py` (new) | Key parsing (`cron_job_id_from_session_id`, `channel_from_origin`), the lineage query (`attribute_sessions`), aggregation (`aggregate`), and `reprice`. No CLI imports. |
| `hermes_cli/subcommands/costs.py` (new) | `build_costs_parser`, `run_costs(args, db) -> int`, `format_table`, `format_csv`, `format_json`. |
| `hermes_cli/main.py` (modify) | `cmd_costs` glue + registration beside `build_insights_parser`; `"costs"` in the two command lists. |
| `agent/usage_pricing.py` (modify) | `_load_pricing_overrides()` and the override lookup at the top of `get_pricing_entry`. |
| `tests/agent/test_cost_attribution.py` (new) | Keys, lineage, aggregation, reprice. |
| `tests/agent/test_usage_pricing_overrides.py` (new) | Override lookup. |
| `tests/hermes_cli/test_costs_cli.py` (new) | Parser, formatters, exit codes. |
| `tests/test_fork_feature_inventory.py` (modify) | Needles for the three wiring points. |
| `CLAUDE.md` (modify) | Fork section entry. |
| `docs/superpowers/specs/2026-09-17-cost-attribution-design.md` (modify) | One wording fix, see Task 7. |

Shared test fixture (repeated verbatim in each new test file so each file stands alone):

```python
import json
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB

DAY = 86400


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "costs.db")
    yield session_db
    session_db.close()


def _backdate(db, session_id: str, started_at: float) -> None:
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, session_id))
    db._conn.commit()


def _seed(db, session_id, *, source="slack", started_at=None, user_id=None,
          chat_id=None, chat_type=None, parent=None, origin=None, display_name=None,
          model="gpt-5.4-mini", input_tokens=1000, output_tokens=100,
          cost=None, status="estimated", actual=None):
    """Create a session with tokens and an (estimated|actual|unknown) cost."""
    db.create_session(
        session_id=session_id, source=source, model=model, user_id=user_id,
        chat_id=chat_id, chat_type=chat_type, parent_session_id=parent,
        origin_json=json.dumps(origin) if origin else None, display_name=display_name,
    )
    db.update_token_counts(
        session_id, input_tokens=input_tokens, output_tokens=output_tokens,
        model=model, billing_provider="openai",
        estimated_cost_usd=cost, actual_cost_usd=actual,
        cost_status=status, cost_source="official_docs_snapshot" if cost is not None else "none",
        api_call_count=1,
    )
    db.append_message(session_id, role="user", content="hi")
    _backdate(db, session_id, started_at if started_at is not None else time.time() - DAY)
```

Note for implementers: `update_token_counts` stores `estimated_cost_usd = COALESCE(?, 0)`, so an unknown-cost row has cost `0`, not NULL. "Priced" therefore means `cost_status IN ('actual', 'estimated', 'included')`, never "cost is non-null".

---

### Task 1: Key parsing helpers

**Files:**
- Create: `agent/cost_attribution.py`
- Test: `tests/agent/test_cost_attribution.py`

**Interfaces:**
- Produces:
  - `cron_job_id_from_session_id(session_id: str | None) -> str | None`
  - `@dataclass(frozen=True) class ChannelKey: platform: str; chat_id: str; name: str` with `label` property → `"{platform}:{name}"` (name falls back to chat_id).
  - `channel_from_origin(*, source: str | None, chat_id: str | None, chat_type: str | None, user_id: str | None, origin_json: str | None, display_name: str | None) -> ChannelKey | None`
  - `is_thread_origin(origin_json: str | None) -> bool` (true when `parent_chat_id` present).

- [ ] **Step 1: Write the failing tests**

```python
# tests/agent/test_cost_attribution.py
import json

import pytest

from agent.cost_attribution import (
    ChannelKey,
    channel_from_origin,
    cron_job_id_from_session_id,
    is_thread_origin,
)


class TestCronJobId:
    def test_twelve_hex_id(self):
        assert cron_job_id_from_session_id("cron_3f9a1c2d4e5b_20260917_081500") == "3f9a1c2d4e5b"

    def test_hand_written_id_with_underscores(self):
        assert cron_job_id_from_session_id("cron_daily_digest_v2_20260917_081500") == "daily_digest_v2"

    def test_non_cron_id(self):
        assert cron_job_id_from_session_id("slack_C123_1700000000") is None

    def test_missing_timestamp_is_not_cron(self):
        assert cron_job_id_from_session_id("cron_3f9a1c2d4e5b") is None

    def test_none(self):
        assert cron_job_id_from_session_id(None) is None


class TestChannelFromOrigin:
    def test_channel_session(self):
        origin = {"platform": "slack", "chat_id": "C123", "chat_type": "channel", "chat_name": "issues"}
        key = channel_from_origin(source="slack", chat_id="C123", chat_type="channel", user_id="U1",
                                  origin_json=json.dumps(origin), display_name="issues")
        assert key == ChannelKey(platform="slack", chat_id="C123", name="issues")
        assert key.label == "slack:issues"

    def test_thread_rolls_up_to_parent_channel(self):
        origin = {"platform": "slack", "chat_id": "C123:1700.1", "chat_type": "thread",
                  "parent_chat_id": "C123", "chat_name": "issues"}
        key = channel_from_origin(source="slack", chat_id="C123:1700.1", chat_type="thread", user_id="U1",
                                  origin_json=json.dumps(origin), display_name="issues")
        assert key.chat_id == "C123"
        assert key.platform == "slack"
        assert is_thread_origin(json.dumps(origin))

    def test_dm_keys_on_user(self):
        origin = {"platform": "slack", "chat_id": "D999", "chat_type": "dm"}
        key = channel_from_origin(source="slack", chat_id="D999", chat_type="dm", user_id="U42",
                                  origin_json=json.dumps(origin), display_name=None)
        assert key == ChannelKey(platform="slack", chat_id="dm:U42", name="dm:U42")

    def test_no_origin_falls_back_to_columns(self):
        key = channel_from_origin(source="slack", chat_id="C7", chat_type="channel", user_id=None,
                                  origin_json=None, display_name="general")
        assert key == ChannelKey(platform="slack", chat_id="C7", name="general")

    def test_nothing_gives_none(self):
        assert channel_from_origin(source="cli", chat_id=None, chat_type=None, user_id=None,
                                   origin_json=None, display_name=None) is None

    def test_malformed_origin_json_is_ignored(self):
        key = channel_from_origin(source="slack", chat_id="C7", chat_type="channel", user_id=None,
                                  origin_json="{not json", display_name=None)
        assert key == ChannelKey(platform="slack", chat_id="C7", name="C7")
        assert not is_thread_origin("{not json")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.cost_attribution'`

- [ ] **Step 3: Write the module with the helpers**

```python
# agent/cost_attribution.py
"""Attribute Hermes spend to Slack channels and cron jobs (fork-only).

Pure query layer over ``SessionDB``: nothing here writes except ``reprice``.
Attribution is derived at query time from columns that already exist —
``parent_session_id`` (lineage), the ``cron_<job>_<ts>`` session id (job),
and ``origin_json`` (channel). See
docs/superpowers/specs/2026-09-17-cost-attribution-design.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

_CRON_ID_RE = re.compile(r"^cron_(?P<job>.+)_\d{8}_\d{6}$")

PRICED_STATUSES = frozenset({"actual", "estimated", "included"})


def cron_job_id_from_session_id(session_id: Optional[str]) -> Optional[str]:
    """Return the job id embedded in a cron root session id, else None."""
    if not session_id:
        return None
    match = _CRON_ID_RE.match(session_id)
    return match.group("job") if match else None


@dataclass(frozen=True)
class ChannelKey:
    platform: str
    chat_id: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.platform}:{self.name or self.chat_id}"


def _parse_origin(origin_json: Optional[str]) -> Dict[str, Any]:
    if not origin_json:
        return {}
    try:
        data = json.loads(origin_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_thread_origin(origin_json: Optional[str]) -> bool:
    return bool(_parse_origin(origin_json).get("parent_chat_id"))


def channel_from_origin(
    *,
    source: Optional[str],
    chat_id: Optional[str],
    chat_type: Optional[str],
    user_id: Optional[str],
    origin_json: Optional[str],
    display_name: Optional[str],
) -> Optional[ChannelKey]:
    """Resolve the channel a session belongs to; threads collapse to their parent."""
    origin = _parse_origin(origin_json)
    platform = str(origin.get("platform") or source or "").strip()
    resolved_chat = origin.get("parent_chat_id") or origin.get("chat_id") or chat_id
    resolved_type = origin.get("chat_type") or chat_type
    if not platform or not resolved_chat:
        return None
    resolved_chat = str(resolved_chat)
    if resolved_type == "dm":
        dm_key = f"dm:{user_id or resolved_chat}"
        return ChannelKey(platform=platform, chat_id=dm_key, name=dm_key)
    name = origin.get("chat_name") or display_name or resolved_chat
    return ChannelKey(platform=platform, chat_id=resolved_chat, name=str(name))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add agent/cost_attribution.py tests/agent/test_cost_attribution.py
git commit -m "feat(costs): cost_attribution key helpers — cron job id and channel from origin

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Lineage query — `attribute_sessions`

**Files:**
- Modify: `agent/cost_attribution.py`
- Test: `tests/agent/test_cost_attribution.py`

**Interfaces:**
- Consumes: Task 1 helpers; `SessionDB._read_ctx()` (context manager yielding a `sqlite3.Connection` with `row_factory = sqlite3.Row`).
- Produces:

```python
@dataclass
class ModelUsage:
    model: str
    provider: str
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True

@dataclass
class AttributedSession:
    root_id: str
    source: str
    started_at: float
    user_id: Optional[str]
    job_id: Optional[str]
    job_name: Optional[str]           # None when job_id is None; "<id> (deleted)" when unknown
    channels: List[ChannelKey]        # 0..n; >1 only for multi-target cron jobs
    sessions: int                     # root + descendants with tokens
    api_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    unpriced_tokens: int              # input+output of unpriced contributing sessions
    all_actual: bool                  # every priced contributing session was 'actual'
    models: Dict[Tuple[str, str], ModelUsage]

    @property
    def status(self) -> str: ...      # actual | estimated | partial | unpriced

JobResolver = Callable[[str], Optional[dict]]                 # job_id -> job dict or None
TargetResolver = Callable[[dict], List[dict]]                 # job dict -> [{platform, chat_id, thread_id?}]

def attribute_sessions(
    db, *, since: float, until: float, platform: Optional[str] = None,
    job_resolver: Optional[JobResolver] = None,
    target_resolver: Optional[TargetResolver] = None,
) -> List[AttributedSession]
```

Default resolvers: `job_resolver` wraps `cron.jobs.load_jobs()` into a dict keyed by `id` (loaded once per call); `target_resolver` is `cron.scheduler._resolve_delivery_targets`. Both wrapped in try/except returning `None`/`[]` so a broken jobs file never breaks the report.

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_cost_attribution.py` (after the fixture block from the file map, which must be added at the top of this file now):

```python
from agent.cost_attribution import attribute_sessions

NOW = 1_800_000_000.0  # fixed "now" so buckets are deterministic
WINDOW = dict(since=NOW - 30 * DAY, until=NOW)

SLACK_ISSUES = {"platform": "slack", "chat_id": "C123", "chat_type": "channel", "chat_name": "issues"}
SLACK_THREAD = {"platform": "slack", "chat_id": "C123:1700.1", "chat_type": "thread",
                "parent_chat_id": "C123", "chat_name": "issues"}


def _jobs(*jobs):
    index = {j["id"]: j for j in jobs}
    return lambda job_id: index.get(job_id)


def _targets_from_job(job):
    return job.get("_targets", [])


class TestAttributeSessions:
    def test_channel_session_is_keyed_on_channel(self, db):
        _seed(db, "s1", started_at=NOW - DAY, chat_id="C123", chat_type="channel",
              origin=SLACK_ISSUES, display_name="issues", cost=0.5, user_id="U1")
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        assert len(rows) == 1
        r = rows[0]
        assert r.root_id == "s1"
        assert r.job_id is None
        assert [c.label for c in r.channels] == ["slack:issues"]
        assert r.cost_usd == pytest.approx(0.5)
        assert r.status == "estimated"
        assert r.user_id == "U1"

    def test_thread_and_subagent_roll_up_to_root(self, db):
        _seed(db, "root", started_at=NOW - DAY, chat_id="C123:1700.1", chat_type="thread",
              origin=SLACK_THREAD, cost=0.5)
        _seed(db, "child", source="subagent", started_at=NOW - DAY + 10, parent="root", cost=0.25)
        _seed(db, "grandchild", source="subagent", started_at=NOW - DAY + 20, parent="child", cost=0.25)
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        assert len(rows) == 1
        r = rows[0]
        assert r.root_id == "root"
        assert r.channels[0].chat_id == "C123"
        assert r.sessions == 3
        assert r.cost_usd == pytest.approx(1.0)
        assert r.input_tokens == 3000

    def test_orphan_child_is_unattributed(self, db):
        _seed(db, "orphan", source="subagent", started_at=NOW - DAY, parent="gone", cost=0.1)
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        assert len(rows) == 1
        assert rows[0].job_id is None and rows[0].channels == []

    def test_cron_root_gets_job_and_delivery_channels(self, db):
        _seed(db, "cron_ab12_20260901_080000", source="cron", started_at=NOW - DAY, cost=2.0)
        job = {"id": "ab12", "name": "Daily digest",
               "_targets": [{"platform": "slack", "chat_id": "C123"}, {"platform": "slack", "chat_id": "C456"}]}
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(job), target_resolver=_targets_from_job)
        r = rows[0]
        assert r.job_id == "ab12"
        assert r.job_name == "Daily digest"
        assert sorted(c.chat_id for c in r.channels) == ["C123", "C456"]

    def test_cron_root_of_deleted_job(self, db):
        _seed(db, "cron_dead_20260901_080000", source="cron", started_at=NOW - DAY, cost=1.0)
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        assert rows[0].job_id == "dead"
        assert rows[0].job_name == "dead (deleted)"
        assert rows[0].channels == []

    def test_delivery_channel_name_comes_from_a_session_in_that_channel(self, db):
        _seed(db, "s1", started_at=NOW - 2 * DAY, chat_id="C123", chat_type="channel",
              origin=SLACK_ISSUES, display_name="issues", cost=0.1)
        _seed(db, "cron_ab12_20260901_080000", source="cron", started_at=NOW - DAY, cost=2.0)
        job = {"id": "ab12", "name": "Digest", "_targets": [{"platform": "slack", "chat_id": "C123"}]}
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(job), target_resolver=_targets_from_job)
        cron_row = next(r for r in rows if r.job_id)
        assert cron_row.channels[0].label == "slack:issues"

    def test_status_partial_and_unpriced(self, db):
        _seed(db, "root", started_at=NOW - DAY, chat_id="C123", chat_type="channel",
              origin=SLACK_ISSUES, cost=0.5)
        _seed(db, "child", source="subagent", started_at=NOW - DAY + 1, parent="root",
              cost=None, status="unknown", input_tokens=700, output_tokens=300)
        _seed(db, "lonely", started_at=NOW - DAY, chat_id="C9", chat_type="channel",
              cost=None, status="unknown")
        rows = {r.root_id: r for r in attribute_sessions(db, **WINDOW, job_resolver=_jobs(),
                                                          target_resolver=_targets_from_job)}
        assert rows["root"].status == "partial"
        assert rows["root"].unpriced_tokens == 1000
        assert rows["lonely"].status == "unpriced"

    def test_status_actual_only_when_every_priced_session_is_actual(self, db):
        _seed(db, "a", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=0.1, actual=0.12, status="actual")
        _seed(db, "b", started_at=NOW - DAY, chat_id="C2", chat_type="channel", cost=0.1, status="estimated")
        rows = {r.root_id: r for r in attribute_sessions(db, **WINDOW, job_resolver=_jobs(),
                                                          target_resolver=_targets_from_job)}
        assert rows["a"].status == "actual" and rows["a"].cost_usd == pytest.approx(0.12)
        assert rows["b"].status == "estimated"

    def test_window_filters_on_root_started_at(self, db):
        _seed(db, "old", started_at=NOW - 40 * DAY, chat_id="C1", chat_type="channel", cost=1.0)
        _seed(db, "new", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=1.0)
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        assert [r.root_id for r in rows] == ["new"]

    def test_platform_filter_matches_root_source_or_channel(self, db):
        _seed(db, "cron_ab12_20260901_080000", source="cron", started_at=NOW - DAY, cost=2.0)
        _seed(db, "cli1", source="cli", started_at=NOW - DAY, cost=1.0)
        job = {"id": "ab12", "name": "Digest", "_targets": [{"platform": "slack", "chat_id": "C123"}]}
        rows = attribute_sessions(db, **WINDOW, platform="slack", job_resolver=_jobs(job),
                                  target_resolver=_targets_from_job)
        assert [r.root_id for r in rows] == ["cron_ab12_20260901_080000"]

    def test_zero_token_bare_rows_are_skipped(self, db):
        db.create_session(session_id="bare", source="slack", chat_id="C1", chat_type="channel")
        _backdate(db, "bare", NOW - DAY)
        assert attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job) == []

    def test_models_breakdown_includes_aux_rows(self, db):
        _seed(db, "s1", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=0.5)
        db.record_auxiliary_usage("s1", "vision", model="gpt-4o", billing_provider="openai",
                                  input_tokens=10, output_tokens=5, estimated_cost_usd=0.01)
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        models = rows[0].models
        assert models[("gpt-5.4-mini", "openai")].cost_usd == pytest.approx(0.5)
        assert models[("gpt-4o", "openai")].cost_usd == pytest.approx(0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py`
Expected: FAIL with `ImportError: cannot import name 'attribute_sessions'`

- [ ] **Step 3: Implement the lineage query**

Add to `agent/cost_attribution.py`:

```python
import logging
from dataclasses import field
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ModelUsage:
    model: str
    provider: str
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True


@dataclass
class AttributedSession:
    root_id: str
    source: str
    started_at: float
    user_id: Optional[str]
    job_id: Optional[str]
    job_name: Optional[str]
    channels: List[ChannelKey]
    sessions: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_tokens: int = 0
    all_actual: bool = True
    models: Dict[Tuple[str, str], ModelUsage] = field(default_factory=dict)

    @property
    def priced_tokens(self) -> int:
        return self.input_tokens + self.output_tokens - self.unpriced_tokens

    @property
    def status(self) -> str:
        return status_for(priced_tokens=self.priced_tokens, unpriced_tokens=self.unpriced_tokens,
                          all_actual=self.all_actual)


def status_for(*, priced_tokens: int, unpriced_tokens: int, all_actual: bool) -> str:
    if unpriced_tokens and priced_tokens:
        return "partial"
    if unpriced_tokens or not priced_tokens:
        return "unpriced"
    return "actual" if all_actual else "estimated"


JobResolver = Callable[[str], Optional[dict]]
TargetResolver = Callable[[dict], List[dict]]


def _default_job_resolver() -> JobResolver:
    try:
        from cron.jobs import load_jobs
        index = {str(j.get("id")): j for j in load_jobs() if isinstance(j, dict)}
    except Exception:  # broken jobs file must never break the report
        logger.debug("cost_attribution: cron jobs unavailable", exc_info=True)
        index = {}
    return index.get


def _default_target_resolver(job: dict) -> List[dict]:
    try:
        from cron.scheduler import _resolve_delivery_targets
        return [t for t in _resolve_delivery_targets(job) if isinstance(t, dict)]
    except Exception:
        logger.debug("cost_attribution: delivery targets unavailable", exc_info=True)
        return []


_LINEAGE_SQL = """
WITH RECURSIVE lineage(id, root_id, orphan, depth) AS (
    SELECT id, id,
           CASE WHEN parent_session_id IS NULL THEN 0 ELSE 1 END, 0
      FROM sessions
     WHERE parent_session_id IS NULL
        OR parent_session_id NOT IN (SELECT id FROM sessions)
    UNION ALL
    SELECT s.id, l.root_id, l.orphan, l.depth + 1
      FROM sessions s JOIN lineage l ON s.parent_session_id = l.id
     WHERE l.depth < 100
)
SELECT l.root_id, l.orphan,
       s.id, s.source, s.user_id, s.chat_id, s.chat_type, s.origin_json, s.display_name,
       s.started_at, s.api_call_count,
       COALESCE(s.input_tokens, 0) AS input_tokens,
       COALESCE(s.output_tokens, 0) AS output_tokens,
       COALESCE(s.cache_read_tokens, 0) AS cache_read_tokens,
       COALESCE(s.cache_write_tokens, 0) AS cache_write_tokens,
       s.estimated_cost_usd, s.actual_cost_usd, s.cost_status,
       r.source AS root_source, r.user_id AS root_user, r.started_at AS root_started_at,
       r.chat_id AS root_chat_id, r.chat_type AS root_chat_type,
       r.origin_json AS root_origin_json, r.display_name AS root_display_name
  FROM lineage l
  JOIN sessions s ON s.id = l.id
  JOIN sessions r ON r.id = l.root_id
 WHERE r.started_at >= ? AND r.started_at < ?
 ORDER BY r.started_at, l.root_id, l.depth
"""

_MODEL_USAGE_SQL = """
SELECT u.session_id, u.model, u.billing_provider,
       u.api_call_count, u.input_tokens, u.output_tokens,
       u.cache_read_tokens, u.cache_write_tokens,
       u.estimated_cost_usd, u.actual_cost_usd, u.cost_status
  FROM session_model_usage u
 WHERE u.session_id IN ({placeholders})
"""

_CHANNEL_NAMES_SQL = """
SELECT source, chat_id, origin_json, display_name
  FROM sessions
 WHERE chat_id IS NOT NULL AND (display_name IS NOT NULL OR origin_json IS NOT NULL)
"""


def _session_cost(row) -> float:
    actual = row["actual_cost_usd"]
    if actual is not None:
        return float(actual)
    return float(row["estimated_cost_usd"] or 0.0)


def _channel_name_index(conn) -> Dict[Tuple[str, str], str]:
    """(platform, chat_id) -> display name, from non-thread sessions."""
    names: Dict[Tuple[str, str], str] = {}
    for row in conn.execute(_CHANNEL_NAMES_SQL):
        if is_thread_origin(row["origin_json"]):
            continue
        key = channel_from_origin(source=row["source"], chat_id=row["chat_id"], chat_type=None,
                                  user_id=None, origin_json=row["origin_json"],
                                  display_name=row["display_name"])
        if key and key.name != key.chat_id:
            names.setdefault((key.platform, key.chat_id), key.name)
    return names


def _cron_channels(job: Optional[dict], target_resolver: TargetResolver,
                   names: Dict[Tuple[str, str], str]) -> List[ChannelKey]:
    if not job:
        return []
    channels: List[ChannelKey] = []
    seen = set()
    for target in target_resolver(job):
        platform = str(target.get("platform") or "").strip()
        chat_id = str(target.get("chat_id") or "").strip()
        if not platform or not chat_id or (platform, chat_id) in seen:
            continue
        seen.add((platform, chat_id))
        channels.append(ChannelKey(platform=platform, chat_id=chat_id,
                                   name=names.get((platform, chat_id), chat_id)))
    return channels


def attribute_sessions(
    db,
    *,
    since: float,
    until: float,
    platform: Optional[str] = None,
    job_resolver: Optional[JobResolver] = None,
    target_resolver: Optional[TargetResolver] = None,
) -> List[AttributedSession]:
    """One row per root session started in [since, until), with descendants rolled up."""
    job_resolver = job_resolver or _default_job_resolver()
    target_resolver = target_resolver or _default_target_resolver

    roots: Dict[str, AttributedSession] = {}
    with db._read_ctx() as conn:
        names = _channel_name_index(conn)
        rows = conn.execute(_LINEAGE_SQL, (since, until)).fetchall()
        for row in rows:
            root_id = row["root_id"]
            agg = roots.get(root_id)
            if agg is None:
                job_id = None if row["orphan"] else cron_job_id_from_session_id(root_id)
                job = job_resolver(job_id) if job_id else None
                job_name = None
                if job_id:
                    job_name = str(job.get("name") or job_id) if job else f"{job_id} (deleted)"
                if row["orphan"]:
                    channels: List[ChannelKey] = []
                elif job_id:
                    channels = _cron_channels(job, target_resolver, names)
                else:
                    key = channel_from_origin(
                        source=row["root_source"], chat_id=row["root_chat_id"],
                        chat_type=row["root_chat_type"], user_id=row["root_user"],
                        origin_json=row["root_origin_json"], display_name=row["root_display_name"],
                    )
                    if key is not None:
                        key = ChannelKey(key.platform, key.chat_id,
                                         names.get((key.platform, key.chat_id), key.name))
                    channels = [key] if key else []
                agg = AttributedSession(
                    root_id=root_id, source=row["root_source"] or "", started_at=float(row["root_started_at"]),
                    user_id=row["root_user"], job_id=job_id, job_name=job_name, channels=channels,
                )
                roots[root_id] = agg

            tokens = int(row["input_tokens"]) + int(row["output_tokens"])
            if tokens == 0 and not row["api_call_count"]:
                continue  # bare gateway row, nothing consumed
            agg.sessions += 1
            agg.api_calls += int(row["api_call_count"] or 0)
            agg.input_tokens += int(row["input_tokens"])
            agg.output_tokens += int(row["output_tokens"])
            agg.cache_read_tokens += int(row["cache_read_tokens"])
            agg.cache_write_tokens += int(row["cache_write_tokens"])
            status = row["cost_status"]
            if status in PRICED_STATUSES:
                agg.cost_usd += _session_cost(row)
                if status != "actual":
                    agg.all_actual = False
            else:
                agg.unpriced_tokens += tokens

        session_ids = [row["id"] for row in rows]
        root_of = {row["id"]: row["root_id"] for row in rows}
        for chunk_start in range(0, len(session_ids), 500):
            chunk = session_ids[chunk_start:chunk_start + 500]
            sql = _MODEL_USAGE_SQL.format(placeholders=",".join("?" * len(chunk)))
            for urow in conn.execute(sql, chunk):
                agg = roots[root_of[urow["session_id"]]]
                key = (urow["model"] or "", urow["billing_provider"] or "")
                mu = agg.models.setdefault(key, ModelUsage(model=key[0], provider=key[1]))
                mu.api_calls += int(urow["api_call_count"] or 0)
                mu.input_tokens += int(urow["input_tokens"] or 0)
                mu.output_tokens += int(urow["output_tokens"] or 0)
                mu.cache_read_tokens += int(urow["cache_read_tokens"] or 0)
                mu.cache_write_tokens += int(urow["cache_write_tokens"] or 0)
                if urow["cost_status"] in PRICED_STATUSES or (urow["estimated_cost_usd"] or 0) > 0:
                    mu.cost_usd += _session_cost(urow)
                else:
                    mu.priced = False

    result = [r for r in roots.values() if r.sessions > 0]
    if platform:
        wanted = platform.strip().lower()
        result = [r for r in result
                  if (r.source or "").lower() == wanted or any(c.platform.lower() == wanted for c in r.channels)]
    result.sort(key=lambda r: (r.started_at, r.root_id))
    return result
```

Note the aux-row rule: `record_auxiliary_usage` writes `cost_status=None` but a real `estimated_cost_usd`, so a model-usage row counts as priced when its status is priced **or** its estimate is non-zero.

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py`
Expected: all pass (12 from Task 1 + 12 new)

If `test_delivery_channel_name_comes_from_a_session_in_that_channel` fails because `create_session` did not persist `display_name`, check `SessionDB.create_session` accepts `display_name=` (it does, see `hermes_state.py` around line 6282) and that `_seed` passes it.

- [ ] **Step 5: Commit**

```bash
git add agent/cost_attribution.py tests/agent/test_cost_attribution.py
git commit -m "feat(costs): attribute_sessions — lineage walk, job + channel keys, status rollup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Aggregation — views and buckets

**Files:**
- Modify: `agent/cost_attribution.py`
- Test: `tests/agent/test_cost_attribution.py`

**Interfaces:**
- Consumes: `AttributedSession`, `status_for` from Task 2.
- Produces:

```python
VIEWS = ("channel", "job", "both", "model", "user")
BUCKETS = ("none", "day", "week", "month")
NONE_LABEL = "(none)"

@dataclass
class ReportRow:
    period: Optional[str]            # None when bucket == "none"
    keys: Dict[str, str]             # column name -> value, e.g. {"job": ..., "channel": ...}
    cost_usd: float = 0.0
    sessions: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    all_actual: bool = True
    @property
    def status(self) -> str

@dataclass
class Report:
    by: str
    bucket: str
    since: float
    until: float
    key_columns: List[str]
    rows: List[ReportRow]
    total: ReportRow
    double_counted_usd: float        # >0 only for the channel view

def period_label(started_at: float, bucket: str) -> Optional[str]
def aggregate(sessions: List[AttributedSession], *, by: str, bucket: str = "none",
              since: float, until: float, top: int = 50) -> Report
```

Key columns per view: `channel` → `["channel"]`; `job` → `["job"]`; `both` → `["job", "channel"]`; `model` → `["model", "provider"]`; `user` → `["user"]`. `top=0` means all rows. Rows sort by `(period, -cost_usd, keys)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_cost_attribution.py`:

```python
from agent.cost_attribution import (
    AttributedSession, ModelUsage, ReportRow, aggregate, period_label,
)


def _row(root_id, *, cost, job=None, channels=(), started_at=NOW - DAY, user="U1",
         unpriced=0, all_actual=False, tokens=1000, models=None):
    return AttributedSession(
        root_id=root_id, source="slack", started_at=started_at, user_id=user,
        job_id=job, job_name=(f"Job {job}" if job else None),
        channels=[ChannelKey("slack", c, c) for c in channels],
        sessions=1, api_calls=1, input_tokens=tokens, output_tokens=0,
        cost_usd=cost, unpriced_tokens=unpriced, all_actual=all_actual,
        models=models or {},
    )


class TestPeriodLabel:
    def test_labels(self):
        ts = 1_758_067_200.0  # 2025-09-17 00:00:00 UTC (a Wednesday)
        assert period_label(ts, "day") == "2025-09-17"
        assert period_label(ts, "week") == "2025-W38"
        assert period_label(ts, "month") == "2025-09"
        assert period_label(ts, "none") is None


class TestAggregate:
    def test_both_is_a_partition_and_multi_target_joins_channels(self):
        rows = [
            _row("a", cost=1.0, job="j1", channels=("C1", "C2")),
            _row("b", cost=0.5, channels=("C1",)),
            _row("c", cost=0.25),
        ]
        rep = aggregate(rows, by="both", since=0, until=NOW)
        assert rep.key_columns == ["job", "channel"]
        keyed = {(r.keys["job"], r.keys["channel"]): r.cost_usd for r in rep.rows}
        assert keyed == {("Job j1", "slack:C1+slack:C2"): 1.0, ("(none)", "slack:C1"): 0.5,
                         ("(none)", "(none)"): 0.25}
        assert rep.total.cost_usd == pytest.approx(1.75)
        assert rep.double_counted_usd == 0

    def test_channel_view_double_counts_and_reports_it(self):
        rows = [_row("a", cost=1.0, job="j1", channels=("C1", "C2")), _row("b", cost=0.5, channels=("C1",))]
        rep = aggregate(rows, by="channel", since=0, until=NOW)
        keyed = {r.keys["channel"]: r.cost_usd for r in rep.rows}
        assert keyed == {"slack:C1": 1.5, "slack:C2": 1.0}
        assert rep.total.cost_usd == pytest.approx(1.5)      # true spend, not the sum of rows
        assert rep.double_counted_usd == pytest.approx(1.0)

    def test_job_view_and_none_bucket(self):
        rows = [_row("a", cost=1.0, job="j1"), _row("b", cost=2.0, job="j1"), _row("c", cost=0.5)]
        rep = aggregate(rows, by="job", since=0, until=NOW)
        assert [(r.keys["job"], r.cost_usd) for r in rep.rows] == [("Job j1", 3.0), ("(none)", 0.5)]
        assert rep.rows[0].sessions == 2
        assert all(r.period is None for r in rep.rows)

    def test_day_bucket_splits_and_orders(self):
        d1 = 1_758_067_200.0
        rows = [_row("a", cost=1.0, job="j1", started_at=d1), _row("b", cost=2.0, job="j1", started_at=d1 + DAY)]
        rep = aggregate(rows, by="job", bucket="day", since=0, until=NOW)
        assert [(r.period, r.cost_usd) for r in rep.rows] == [("2025-09-17", 1.0), ("2025-09-18", 2.0)]

    def test_status_merges_across_roots(self):
        rows = [_row("a", cost=1.0, all_actual=True), _row("b", cost=0.0, unpriced=1000)]
        rep = aggregate(rows, by="user", since=0, until=NOW)
        assert rep.rows[0].keys["user"] == "U1"
        assert rep.rows[0].status == "partial"
        assert rep.rows[0].unpriced_tokens == 1000
        assert rep.total.status == "partial"

    def test_all_actual_status(self):
        rep = aggregate([_row("a", cost=1.0, all_actual=True)], by="user", since=0, until=NOW)
        assert rep.rows[0].status == "actual"

    def test_model_view_reads_model_usage(self):
        models = {("gpt-5.4-mini", "openai"): ModelUsage("gpt-5.4-mini", "openai", api_calls=2,
                                                          input_tokens=100, cost_usd=0.4),
                  ("gpt-4o", "openai"): ModelUsage("gpt-4o", "openai", api_calls=1, input_tokens=10, cost_usd=0.1)}
        rep = aggregate([_row("a", cost=0.5, models=models)], by="model", since=0, until=NOW)
        assert rep.key_columns == ["model", "provider"]
        assert [(r.keys["model"], r.cost_usd) for r in rep.rows] == [("gpt-5.4-mini", 0.4), ("gpt-4o", 0.1)]
        assert rep.rows[0].api_calls == 2

    def test_top_limits_rows_but_not_total(self):
        rows = [_row(f"r{i}", cost=float(i), channels=(f"C{i}",)) for i in range(1, 6)]
        rep = aggregate(rows, by="channel", since=0, until=NOW, top=2)
        assert [r.keys["channel"] for r in rep.rows] == ["slack:C5", "slack:C4"]
        assert rep.total.cost_usd == pytest.approx(15.0)

    def test_unknown_view_raises(self):
        with pytest.raises(ValueError):
            aggregate([], by="nope", since=0, until=NOW)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py`
Expected: FAIL with `ImportError: cannot import name 'aggregate'`

- [ ] **Step 3: Implement aggregation**

Add to `agent/cost_attribution.py`:

```python
from datetime import datetime, timezone

VIEWS = ("channel", "job", "both", "model", "user")
BUCKETS = ("none", "day", "week", "month")
NONE_LABEL = "(none)"
_KEY_COLUMNS = {
    "channel": ["channel"], "job": ["job"], "both": ["job", "channel"],
    "model": ["model", "provider"], "user": ["user"],
}


def period_label(started_at: float, bucket: str) -> Optional[str]:
    if bucket == "none":
        return None
    dt = datetime.fromtimestamp(started_at, tz=timezone.utc)
    if bucket == "day":
        return dt.strftime("%Y-%m-%d")
    if bucket == "week":
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if bucket == "month":
        return dt.strftime("%Y-%m")
    raise ValueError(f"unknown bucket {bucket!r}; expected one of {BUCKETS}")


@dataclass
class ReportRow:
    period: Optional[str]
    keys: Dict[str, str]
    cost_usd: float = 0.0
    sessions: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    all_actual: bool = True

    @property
    def status(self) -> str:
        return status_for(priced_tokens=self.priced_tokens, unpriced_tokens=self.unpriced_tokens,
                          all_actual=self.all_actual)

    def add(self, *, cost_usd: float, sessions: int, api_calls: int, input_tokens: int,
            output_tokens: int, cache_read_tokens: int, cache_write_tokens: int,
            priced_tokens: int, unpriced_tokens: int, all_actual: bool) -> None:
        self.cost_usd += cost_usd
        self.sessions += sessions
        self.api_calls += api_calls
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens
        self.priced_tokens += priced_tokens
        self.unpriced_tokens += unpriced_tokens
        if priced_tokens and not all_actual:
            self.all_actual = False


@dataclass
class Report:
    by: str
    bucket: str
    since: float
    until: float
    key_columns: List[str]
    rows: List[ReportRow]
    total: ReportRow
    double_counted_usd: float = 0.0


def _session_contribution(s: AttributedSession) -> dict:
    return dict(cost_usd=s.cost_usd, sessions=s.sessions, api_calls=s.api_calls,
                input_tokens=s.input_tokens, output_tokens=s.output_tokens,
                cache_read_tokens=s.cache_read_tokens, cache_write_tokens=s.cache_write_tokens,
                priced_tokens=s.priced_tokens, unpriced_tokens=s.unpriced_tokens, all_actual=s.all_actual)


def _model_contribution(m: ModelUsage) -> dict:
    tokens = m.input_tokens + m.output_tokens
    return dict(cost_usd=m.cost_usd, sessions=0, api_calls=m.api_calls,
                input_tokens=m.input_tokens, output_tokens=m.output_tokens,
                cache_read_tokens=m.cache_read_tokens, cache_write_tokens=m.cache_write_tokens,
                priced_tokens=tokens if m.priced else 0, unpriced_tokens=0 if m.priced else tokens,
                all_actual=False)


def _keys_for(s: AttributedSession, by: str) -> List[Tuple[Dict[str, str], dict]]:
    """Return [(keys, contribution)] — several entries only for the channel view."""
    job = s.job_name or NONE_LABEL
    if by == "job":
        return [({"job": job}, _session_contribution(s))]
    if by == "user":
        return [({"user": s.user_id or NONE_LABEL}, _session_contribution(s))]
    if by == "both":
        channel = "+".join(c.label for c in s.channels) or NONE_LABEL
        return [({"job": job, "channel": channel}, _session_contribution(s))]
    if by == "channel":
        labels = [c.label for c in s.channels] or [NONE_LABEL]
        return [({"channel": label}, _session_contribution(s)) for label in labels]
    if by == "model":
        return [({"model": m.model or NONE_LABEL, "provider": m.provider or NONE_LABEL}, _model_contribution(m))
                for m in s.models.values()]
    raise ValueError(f"unknown view {by!r}; expected one of {VIEWS}")


def aggregate(sessions: List[AttributedSession], *, by: str, bucket: str = "none",
              since: float, until: float, top: int = 50) -> Report:
    if by not in VIEWS:
        raise ValueError(f"unknown view {by!r}; expected one of {VIEWS}")
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {BUCKETS}")
    key_columns = _KEY_COLUMNS[by]
    rows: Dict[Tuple[Optional[str], Tuple[str, ...]], ReportRow] = {}
    total = ReportRow(period=None, keys={})
    double_counted = 0.0
    for s in sessions:
        period = period_label(s.started_at, bucket)
        entries = _keys_for(s, by)
        for keys, contribution in entries:
            slot = (period, tuple(keys[c] for c in key_columns))
            row = rows.get(slot)
            if row is None:
                row = rows[slot] = ReportRow(period=period, keys=keys)
            row.add(**contribution)
        if by == "model":
            for _, contribution in entries:
                total.add(**contribution)
        else:
            total.add(**_session_contribution(s))
            if by == "channel" and len(entries) > 1:
                double_counted += s.cost_usd * (len(entries) - 1)
    ordered = sorted(rows.values(),
                     key=lambda r: (r.period or "", -r.cost_usd, tuple(r.keys[c] for c in key_columns)))
    if top and top > 0:
        ordered = ordered[:top]
    return Report(by=by, bucket=bucket, since=since, until=until, key_columns=key_columns,
                  rows=ordered, total=total, double_counted_usd=double_counted)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add agent/cost_attribution.py tests/agent/test_cost_attribution.py
git commit -m "feat(costs): aggregate — channel/job/both/model/user views with day/week/month buckets

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Pricing overrides in `get_pricing_entry`

**Files:**
- Modify: `agent/usage_pricing.py:1263-1290` (`get_pricing_entry`) and add `_load_pricing_overrides` just above it.
- Test: `tests/agent/test_usage_pricing_overrides.py`

**Interfaces:**
- Consumes: `hermes_cli.config.read_raw_config() -> dict` (imported lazily inside the function so tests can monkeypatch `hermes_cli.config.read_raw_config`); `PricingEntry`, `resolve_billing_route`, `_to_decimal` (existing, `agent/usage_pricing.py:~1039`, returns `Optional[Decimal]`).
- Produces: `_load_pricing_overrides() -> Dict[str, PricingEntry]` keyed by lower-cased model name; `pricing_override_for(model_name: str, route: BillingRoute) -> Optional[PricingEntry]`.

Config shape (from the spec):

```yaml
pricing:
  overrides:
    gpt-5.4-mini: {input: 0.40, output: 1.60, cache_read: 0.10, cache_write: 0.0}
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/agent/test_usage_pricing_overrides.py
from decimal import Decimal

import pytest

import hermes_cli.config as hermes_config
from agent import usage_pricing
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry


@pytest.fixture()
def overrides(monkeypatch):
    state = {"pricing": {"overrides": {}}}

    def set_overrides(mapping):
        state["pricing"]["overrides"] = mapping

    monkeypatch.setattr(hermes_config, "read_raw_config", lambda: state)
    return set_overrides


def test_no_block_means_no_override(overrides):
    overrides({})
    assert get_pricing_entry("gpt-5.4-mini", provider="openai", base_url="https://api.openai.com/v1") is None


def test_exact_match_prices_unknown_model(overrides):
    overrides({"gpt-5.4-mini": {"input": 0.40, "output": 1.60, "cache_read": 0.10}})
    entry = get_pricing_entry("gpt-5.4-mini", provider="openai", base_url="https://api.openai.com/v1")
    assert entry is not None
    assert entry.source == "user_override"
    assert entry.pricing_version == "user-override"
    assert entry.input_cost_per_million == Decimal("0.40")
    assert entry.cache_read_cost_per_million == Decimal("0.10")
    assert entry.cache_write_cost_per_million == Decimal("0")


def test_vendor_prefix_and_case_are_tolerated(overrides):
    overrides({"gpt-5.4-mini": {"input": 1, "output": 2}})
    assert get_pricing_entry("openai/GPT-5.4-Mini", provider="openai").source == "user_override"


def test_override_wins_over_catalog(overrides):
    overrides({"gpt-4o": {"input": 99, "output": 99}})
    entry = get_pricing_entry("gpt-4o", provider="openai", base_url="https://api.openai.com/v1")
    assert entry.source == "user_override" and entry.input_cost_per_million == Decimal("99")


def test_malformed_entry_is_ignored(overrides):
    overrides({"gpt-5.4-mini": {"input": "lots", "output": 1}, "other": "nope"})
    assert get_pricing_entry("gpt-5.4-mini", provider="openai", base_url="https://api.openai.com/v1") is None


def test_estimate_uses_override(overrides):
    overrides({"gpt-5.4-mini": {"input": 1.0, "output": 2.0}})
    result = estimate_usage_cost("gpt-5.4-mini", CanonicalUsage(input_tokens=1_000_000, output_tokens=500_000),
                                 provider="openai", base_url="https://api.openai.com/v1")
    assert result.status == "estimated"
    assert result.source == "user_override"
    assert result.amount_usd == Decimal("2.0")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_usage_pricing_overrides.py`
Expected: `test_exact_match_prices_unknown_model`, `test_vendor_prefix_and_case_are_tolerated`, `test_override_wins_over_catalog`, `test_estimate_uses_override` FAIL (entry is None / source is not `user_override`); the two negative tests pass already.

- [ ] **Step 3: Implement the override lookup**

In `agent/usage_pricing.py`, add immediately above `def get_pricing_entry(`:

```python
_OVERRIDE_RATE_KEYS = {
    "input": "input_cost_per_million",
    "output": "output_cost_per_million",
    "cache_read": "cache_read_cost_per_million",
    "cache_write": "cache_write_cost_per_million",
}


def _load_pricing_overrides() -> Dict[str, PricingEntry]:
    """Parse ``pricing.overrides`` from config.yaml into PricingEntry values.

    Fork addition: the catalog below is a snapshot and cannot know every
    model an operator runs (the VM's ``gpt-5.4-mini`` is absent), so an
    operator can state USD-per-million rates directly. Malformed entries are
    skipped (logged once per process) — never an exception in the request path.
    """
    try:
        from hermes_cli.config import read_raw_config
        block = read_raw_config().get("pricing") or {}
    except Exception:
        logger.debug("pricing overrides unavailable", exc_info=True)
        return {}
    raw = block.get("overrides") if isinstance(block, dict) else None
    if not isinstance(raw, dict):
        return {}
    entries: Dict[str, PricingEntry] = {}
    for model, rates in raw.items():
        if not isinstance(rates, dict) or "input" not in rates or "output" not in rates:
            _warn_bad_override(model)
            continue
        fields: Dict[str, Decimal] = {}
        ok = True
        for key, attr in _OVERRIDE_RATE_KEYS.items():
            value = rates.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                ok = False
                break
            dec = _to_decimal(value)
            if dec is None or dec < 0:
                ok = False
                break
            fields[attr] = dec
        if not ok:
            _warn_bad_override(model)
            continue
        entries[str(model).strip().lower()] = PricingEntry(
            source="user_override", pricing_version="user-override", **fields
        )
    return entries


_BAD_OVERRIDES_WARNED: set = set()


def _warn_bad_override(model: Any) -> None:
    key = str(model)
    if key not in _BAD_OVERRIDES_WARNED:
        _BAD_OVERRIDES_WARNED.add(key)
        logger.warning("pricing.overrides[%r] ignored: expected {input, output[, cache_read, cache_write]} numbers", key)


def pricing_override_for(model_name: str, route: BillingRoute) -> Optional[PricingEntry]:
    overrides = _load_pricing_overrides()
    if not overrides:
        return None
    candidates = []
    for name in (model_name, route.model):
        if not name:
            continue
        lowered = name.strip().lower()
        candidates.append(lowered)
        if "/" in lowered:
            candidates.append(lowered.split("/", 1)[1])
    for candidate in candidates:
        entry = overrides.get(candidate)
        if entry is not None:
            return entry
    return None
```

Then change the top of `get_pricing_entry`:

```python
def get_pricing_entry(
    model_name: str,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[PricingEntry]:
    route = resolve_billing_route(model_name, provider=provider, base_url=base_url)
    override = pricing_override_for(model_name, route)   # fork: operator-stated rates win
    if override is not None:
        return override
    if route.billing_mode == "subscription_included":
        ...  # unchanged from here down
```

Check `logger` exists at module top of `usage_pricing.py` (`logger = logging.getLogger(__name__)`); add it if missing. Check `_to_decimal` handles `str` (it is used for OpenRouter JSON strings, so it does). `Any` and `Dict` are already imported from `typing` in that module; verify with a grep before assuming.

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/agent/test_usage_pricing_overrides.py`
Expected: 6 passed

Also run the existing pricing tests to prove nothing regressed:
Run: `scripts/run_tests.sh tests/agent/test_usage_pricing.py`
Expected: all pass (the override lookup returns None when no block is configured; the autouse fixture points `HERMES_HOME` at a temp dir with no config file).

- [ ] **Step 5: Commit**

```bash
git add agent/usage_pricing.py tests/agent/test_usage_pricing_overrides.py
git commit -m "feat(pricing): pricing.overrides config — operator-stated per-million rates win over the catalog

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `reprice` — price stored unknown-cost history

**Files:**
- Modify: `agent/cost_attribution.py`
- Test: `tests/agent/test_cost_attribution.py`

**Interfaces:**
- Consumes: `estimate_usage_cost(model, CanonicalUsage, provider=, base_url=) -> CostResult` (`amount_usd: Decimal | None`, `status`, `source`, `pricing_version`); `SessionDB._execute_write(fn)` where `fn(conn)` runs inside one write transaction; `SessionDB._read_ctx()`.
- Produces:

```python
@dataclass
class RepriceResult:
    usage_rows_priced: int = 0
    sessions_updated: int = 0
    sessions_priced_from_summary: int = 0   # legacy sessions with no usage rows
    skipped_unknown: int = 0                # still no pricing entry
    added_usd: float = 0.0
    dry_run: bool = False

def reprice(db, *, since: float, until: float, dry_run: bool = False) -> RepriceResult
```

Rules: a `session_model_usage` row is a candidate when `cost_status IS NULL OR cost_status = 'unknown'`, `actual_cost_usd` is NULL or 0, `estimated_cost_usd` is NULL or 0, and `input_tokens + output_tokens > 0`. Aux rows (`task != ''`) with a non-zero estimate are already priced by the aux path and are skipped by that rule. Sessions whose `cost_status` is NULL/unknown and that have **no** main-loop usage row (`task = ''`) are priced from the session's own columns. After any main-loop row changes, the session summary is re-summed from `task = ''` rows: `estimated_cost_usd = SUM(estimated_cost_usd)`, `cost_status = 'estimated'`, `cost_source`/`pricing_version` from the entry. Priced rows are never touched.

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_cost_attribution.py`:

```python
import hermes_cli.config as hermes_config
from agent.cost_attribution import reprice


@pytest.fixture()
def priced_mini(monkeypatch):
    cfg = {"pricing": {"overrides": {"gpt-5.4-mini": {"input": 1.0, "output": 2.0}}}}
    monkeypatch.setattr(hermes_config, "read_raw_config", lambda: cfg)


def _session_cost_row(db, session_id):
    return db._conn.execute(
        "SELECT estimated_cost_usd, cost_status, cost_source FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()


class TestReprice:
    def test_prices_unknown_rows_and_resums_session(self, db, priced_mini):
        _seed(db, "u1", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              cost=None, status="unknown", input_tokens=1_000_000, output_tokens=500_000)
        result = reprice(db, **WINDOW)
        assert result.usage_rows_priced == 1
        assert result.sessions_updated == 1
        assert result.added_usd == pytest.approx(2.0)
        row = _session_cost_row(db, "u1")
        assert row["estimated_cost_usd"] == pytest.approx(2.0)
        assert row["cost_status"] == "estimated"
        assert row["cost_source"] == "user_override"
        rows = attribute_sessions(db, **WINDOW, job_resolver=lambda _j: None, target_resolver=lambda _j: [])
        assert rows[0].status == "estimated" and rows[0].cost_usd == pytest.approx(2.0)

    def test_leaves_priced_rows_alone(self, db, priced_mini):
        _seed(db, "p1", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=0.5, status="estimated")
        result = reprice(db, **WINDOW)
        assert result.usage_rows_priced == 0 and result.sessions_updated == 0
        assert _session_cost_row(db, "p1")["estimated_cost_usd"] == pytest.approx(0.5)

    def test_dry_run_writes_nothing(self, db, priced_mini):
        _seed(db, "u1", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              cost=None, status="unknown", input_tokens=1_000_000, output_tokens=0)
        result = reprice(db, **WINDOW, dry_run=True)
        assert result.dry_run and result.usage_rows_priced == 1 and result.added_usd == pytest.approx(1.0)
        assert _session_cost_row(db, "u1")["cost_status"] == "unknown"

    def test_model_without_pricing_is_skipped(self, db, priced_mini):
        _seed(db, "u2", started_at=NOW - DAY, chat_id="C1", chat_type="channel", model="mystery-9",
              cost=None, status="unknown")
        result = reprice(db, **WINDOW)
        assert result.skipped_unknown == 1 and result.usage_rows_priced == 0
        assert _session_cost_row(db, "u2")["cost_status"] == "unknown"

    def test_legacy_session_without_usage_rows(self, db, priced_mini):
        _seed(db, "legacy", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              cost=None, status="unknown", input_tokens=1_000_000, output_tokens=0)
        db._conn.execute("DELETE FROM session_model_usage WHERE session_id = 'legacy'")
        db._conn.commit()
        result = reprice(db, **WINDOW)
        assert result.sessions_priced_from_summary == 1
        assert _session_cost_row(db, "legacy")["estimated_cost_usd"] == pytest.approx(1.0)

    def test_window_respected(self, db, priced_mini):
        _seed(db, "old", started_at=NOW - 60 * DAY, chat_id="C1", chat_type="channel", cost=None, status="unknown")
        assert reprice(db, **WINDOW).usage_rows_priced == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py -k TestReprice`
Expected: FAIL with `ImportError: cannot import name 'reprice'`

- [ ] **Step 3: Implement reprice**

Add to `agent/cost_attribution.py`:

```python
@dataclass
class RepriceResult:
    usage_rows_priced: int = 0
    sessions_updated: int = 0
    sessions_priced_from_summary: int = 0
    skipped_unknown: int = 0
    added_usd: float = 0.0
    dry_run: bool = False


_UNPRICED_USAGE_SQL = """
SELECT u.rowid AS rid, u.session_id, u.model, u.billing_provider, u.billing_base_url, u.task,
       u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens
  FROM session_model_usage u JOIN sessions s ON s.id = u.session_id
 WHERE s.started_at >= ? AND s.started_at < ?
   AND (u.cost_status IS NULL OR u.cost_status = 'unknown')
   AND COALESCE(u.actual_cost_usd, 0) = 0
   AND COALESCE(u.estimated_cost_usd, 0) = 0
   AND (u.input_tokens + u.output_tokens) > 0
 ORDER BY u.session_id
"""

_UNPRICED_LEGACY_SESSIONS_SQL = """
SELECT s.id, s.model, s.billing_provider, s.billing_base_url,
       COALESCE(s.input_tokens, 0) AS input_tokens, COALESCE(s.output_tokens, 0) AS output_tokens,
       COALESCE(s.cache_read_tokens, 0) AS cache_read_tokens, COALESCE(s.cache_write_tokens, 0) AS cache_write_tokens,
       COALESCE(s.reasoning_tokens, 0) AS reasoning_tokens
  FROM sessions s
 WHERE s.started_at >= ? AND s.started_at < ?
   AND (s.cost_status IS NULL OR s.cost_status = 'unknown')
   AND COALESCE(s.actual_cost_usd, 0) = 0
   AND (COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)) > 0
   AND NOT EXISTS (SELECT 1 FROM session_model_usage u WHERE u.session_id = s.id AND u.task = '')
"""


def _estimate(row) -> Optional[tuple]:
    """(amount, source, pricing_version) for a usage/session row, or None when unpriced."""
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
    model = row["model"]
    if not model:
        return None
    usage = CanonicalUsage(
        input_tokens=int(row["input_tokens"] or 0), output_tokens=int(row["output_tokens"] or 0),
        cache_read_tokens=int(row["cache_read_tokens"] or 0), cache_write_tokens=int(row["cache_write_tokens"] or 0),
        reasoning_tokens=int(row["reasoning_tokens"] or 0), request_count=1,
    )
    result = estimate_usage_cost(model, usage, provider=row["billing_provider"] or None,
                                 base_url=row["billing_base_url"] or None)
    if result.status not in PRICED_STATUSES or result.amount_usd is None:
        return None
    return float(result.amount_usd), result.source, result.pricing_version


def reprice(db, *, since: float, until: float, dry_run: bool = False) -> RepriceResult:
    """Price stored rows whose cost is unknown, using current overrides/catalog."""
    result = RepriceResult(dry_run=dry_run)
    with db._read_ctx() as conn:
        usage_rows = [dict(r) for r in conn.execute(_UNPRICED_USAGE_SQL, (since, until))]
        legacy_rows = [dict(r) for r in conn.execute(_UNPRICED_LEGACY_SESSIONS_SQL, (since, until))]

    usage_updates: Dict[str, List[tuple]] = {}   # session_id -> [(rid, amount, source, version, task)]
    for row in usage_rows:
        est = _estimate(row)
        if est is None:
            result.skipped_unknown += 1
            continue
        amount, source, version = est
        usage_updates.setdefault(row["session_id"], []).append((row["rid"], amount, source, version, row["task"]))
        result.usage_rows_priced += 1
        result.added_usd += amount

    legacy_updates: List[tuple] = []             # (session_id, amount, source, version)
    for row in legacy_rows:
        est = _estimate(row)
        if est is None:
            result.skipped_unknown += 1
            continue
        amount, source, version = est
        legacy_updates.append((row["id"], amount, source, version))
        result.sessions_priced_from_summary += 1
        result.added_usd += amount

    result.sessions_updated = len({sid for sid, ups in usage_updates.items() if any(u[4] == "" for u in ups)}) \
        + len(legacy_updates)
    if dry_run:
        return result

    for session_id, updates in usage_updates.items():
        def _do(conn, session_id=session_id, updates=updates):
            for rid, amount, source, version, _task in updates:
                conn.execute(
                    "UPDATE session_model_usage SET estimated_cost_usd = ?, cost_status = 'estimated', "
                    "cost_source = ? WHERE rowid = ?",
                    (amount, source, rid),
                )
            main_loop = [u for u in updates if u[4] == ""]
            if main_loop:
                conn.execute(
                    """UPDATE sessions
                          SET estimated_cost_usd = (SELECT COALESCE(SUM(estimated_cost_usd), 0)
                                                      FROM session_model_usage
                                                     WHERE session_id = ? AND task = ''),
                              cost_status = 'estimated', cost_source = ?, pricing_version = ?
                        WHERE id = ?""",
                    (session_id, main_loop[0][2], main_loop[0][3], session_id),
                )
        db._execute_write(_do)

    for session_id, amount, source, version in legacy_updates:
        def _do_legacy(conn, session_id=session_id, amount=amount, source=source, version=version):
            conn.execute(
                "UPDATE sessions SET estimated_cost_usd = ?, cost_status = 'estimated', "
                "cost_source = ?, pricing_version = ? WHERE id = ?",
                (amount, source, version, session_id),
            )
        db._execute_write(_do_legacy)
    return result
```

If `_execute_write` requires a different call shape (check its signature at `hermes_state.py:5341` before writing), adapt the two call sites; it must run `fn(conn)` inside a committed transaction.

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/agent/test_cost_attribution.py`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add agent/cost_attribution.py tests/agent/test_cost_attribution.py
git commit -m "feat(costs): reprice — price stored unknown-cost rows from current overrides/catalog

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `hermes costs` CLI

**Files:**
- Create: `hermes_cli/subcommands/costs.py`
- Modify: `hermes_cli/main.py` — import beside line 481 (`from hermes_cli.subcommands.insights import build_insights_parser`), `cmd_costs` after `cmd_insights` (ends ~line 12931), registration after line 14586 (`build_insights_parser(subparsers, cmd_insights=cmd_insights)`), `"costs"` in the ordered command list near line 10927 and in `_BUILTIN_SUBCOMMANDS` near line 12367.
- Test: `tests/hermes_cli/test_costs_cli.py`

**Interfaces:**
- Consumes: `attribute_sessions`, `aggregate`, `reprice`, `Report`, `ReportRow`, `VIEWS`, `BUCKETS` from Tasks 2, 3, 5; `format_cost_label(Decimal) -> str` from `agent/usage_pricing.py`.
- Produces:

```python
def build_costs_parser(subparsers, *, cmd_costs: Callable) -> None
def resolve_window(args, *, now: float | None = None) -> tuple[float, float]   # raises ValueError on bad dates
def run_costs(args, db) -> int                                                   # 0 ok, 1 error; prints to stdout
def format_table(report: Report) -> str
def format_csv(report: Report) -> str
def format_json(report: Report) -> str
```

`--profile` is not a flag of this subcommand: the global `hermes --profile NAME` (handled in `hermes_cli/main.py:586`) already selects the store via `HERMES_HOME`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/hermes_cli/test_costs_cli.py
import argparse
import csv
import io
import json
import time

import pytest

from agent.cost_attribution import AttributedSession, ChannelKey, ModelUsage, aggregate
from hermes_cli.subcommands.costs import (
    build_costs_parser, format_csv, format_json, format_table, resolve_window, run_costs,
)
from hermes_state import SessionDB

DAY = 86400
NOW = 1_800_000_000.0


def _parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_costs_parser(sub, cmd_costs=lambda args: 0)
    return parser


def _args(*argv):
    return _parser().parse_args(["costs", *argv])


def _row(root_id, *, cost, job=None, channels=(), started_at=NOW - DAY, unpriced=0):
    return AttributedSession(
        root_id=root_id, source="slack", started_at=started_at, user_id="U1",
        job_id=job, job_name=(f"Job {job}" if job else None),
        channels=[ChannelKey("slack", c, c) for c in channels],
        sessions=1, api_calls=1, input_tokens=1000, output_tokens=0,
        cost_usd=cost, unpriced_tokens=unpriced, all_actual=False,
    )


class TestParser:
    def test_defaults(self):
        a = _args()
        assert (a.days, a.by, a.bucket, a.top, a.json, a.csv, a.reprice, a.dry_run) == \
               (30, "both", "none", 50, False, False, False, False)

    def test_json_and_csv_are_exclusive(self):
        with pytest.raises(SystemExit):
            _args("--json", "--csv")

    def test_by_choices(self):
        assert _args("--by", "channel").by == "channel"
        with pytest.raises(SystemExit):
            _args("--by", "nope")


class TestWindow:
    def test_days(self):
        since, until = resolve_window(_args("--days", "7"), now=NOW)
        assert (since, until) == (NOW - 7 * DAY, NOW)

    def test_since_until_inclusive_utc(self):
        since, until = resolve_window(_args("--since", "2025-09-01", "--until", "2025-09-30"), now=NOW)
        assert since == 1_756_684_800.0           # 2025-09-01T00:00:00Z
        assert until == 1_759_276_800.0           # 2025-10-01T00:00:00Z (inclusive end)

    def test_since_alone_runs_to_now(self):
        since, until = resolve_window(_args("--since", "2025-09-01"), now=NOW)
        assert until == NOW

    def test_bad_date_raises(self):
        with pytest.raises(ValueError):
            resolve_window(_args("--since", "yesterday"), now=NOW)


class TestFormatters:
    def _report(self, by="both"):
        return aggregate([_row("a", cost=1.0, job="j1", channels=("C1",)),
                          _row("b", cost=0.0, unpriced=1000)], by=by, since=NOW - 30 * DAY, until=NOW)

    def test_table_has_keys_total_and_unpriced_column(self):
        text = format_table(self._report())
        assert "Job j1" in text and "slack:C1" in text
        assert "TOTAL" in text
        assert "unpriced_tokens" in text          # column appears because one row is unpriced
        assert "~$1.00" in text

    def test_table_hides_unpriced_column_when_zero(self):
        rep = aggregate([_row("a", cost=1.0, job="j1", channels=("C1",))], by="job", since=0, until=NOW)
        assert "unpriced_tokens" not in format_table(rep)

    def test_table_footer_reports_double_count(self):
        rep = aggregate([_row("a", cost=1.0, job="j1", channels=("C1", "C2"))], by="channel", since=0, until=NOW)
        assert "counted more than once" in format_table(rep)

    def test_csv(self):
        rows = list(csv.DictReader(io.StringIO(format_csv(self._report()))))
        assert rows[0]["job"] == "Job j1" and rows[0]["channel"] == "slack:C1"
        assert set(rows[0]) >= {"cost_usd", "status", "sessions", "input_tokens", "unpriced_tokens"}

    def test_json_shape(self):
        data = json.loads(format_json(self._report()))
        assert set(data) == {"window", "by", "bucket", "rows", "total", "double_counted_usd"}
        assert data["by"] == "both"
        assert data["rows"][0]["keys"] == {"job": "Job j1", "channel": "slack:C1"}
        assert data["total"]["cost_usd"] == pytest.approx(1.0)
        assert data["total"]["status"] == "partial"


class TestRunCosts:
    @pytest.fixture()
    def db(self, tmp_path):
        session_db = SessionDB(db_path=tmp_path / "costs.db")
        session_db.create_session(session_id="s1", source="slack", model="gpt-5.4-mini", chat_id="C1",
                                  chat_type="channel", display_name="general")
        session_db.update_token_counts("s1", input_tokens=10, output_tokens=5, model="gpt-5.4-mini",
                                       billing_provider="openai", estimated_cost_usd=0.25,
                                       cost_status="estimated", cost_source="official_docs_snapshot", api_call_count=1)
        session_db.append_message("s1", role="user", content="hi")
        yield session_db
        session_db.close()

    def test_table_run(self, db, capsys):
        assert run_costs(_args("--by", "channel"), db) == 0
        out = capsys.readouterr().out
        assert "slack:general" in out and "~$0.25" in out

    def test_json_run(self, db, capsys):
        assert run_costs(_args("--json"), db) == 0
        assert json.loads(capsys.readouterr().out)["total"]["sessions"] == 1

    def test_bad_date_exits_1(self, db, capsys):
        assert run_costs(_args("--since", "nope"), db) == 1
        assert "date" in capsys.readouterr().out.lower()

    def test_empty_store_hints_overrides(self, tmp_path, capsys):
        empty = SessionDB(db_path=tmp_path / "empty.db")
        try:
            assert run_costs(_args(), empty) == 0
        finally:
            empty.close()
        assert "no sessions" in capsys.readouterr().out.lower()

    def test_reprice_dry_run(self, db, capsys):
        assert run_costs(_args("--reprice", "--dry-run"), db) == 0
        assert "dry run" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/hermes_cli/test_costs_cli.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_cli.subcommands.costs'`

- [ ] **Step 3: Write the subcommand module**

```python
# hermes_cli/subcommands/costs.py
"""``hermes costs`` — spend attributed to Slack channels and cron jobs (fork-only).

Parser + formatters only; the query layer is ``agent.cost_attribution``.
Handler injected (``cmd_costs``) to avoid importing ``main``.
"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, List, Optional, Tuple

from agent.cost_attribution import BUCKETS, VIEWS, Report, ReportRow, aggregate, attribute_sessions, reprice
from agent.usage_pricing import format_cost_label

DAY = 86400.0
_HINT = ("Hint: models missing from the pricing catalog show as unpriced. Add per-million rates under\n"
         "      pricing.overrides in config.yaml, then run `hermes costs --reprice` to price stored history.")


def build_costs_parser(subparsers, *, cmd_costs: Callable) -> None:
    """Attach the ``costs`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "costs",
        help="Show spend attributed to Slack channels and cron jobs",
        description="Attribute session cost to channels and automations over a time window.",
    )
    window = p.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=30, help="Window ending now (default: 30)")
    window.add_argument("--since", help="Start date YYYY-MM-DD (UTC)")
    p.add_argument("--until", help="End date YYYY-MM-DD (UTC, inclusive; default: now)")
    p.add_argument("--by", choices=VIEWS, default="both", help="Grouping (default: both = job x channel)")
    p.add_argument("--bucket", choices=BUCKETS, default="none", help="Time bucket (default: none)")
    p.add_argument("--platform", help="Only roots on this platform or delivering to it (e.g. slack)")
    p.add_argument("--top", type=int, default=50, help="Max rows (default 50; 0 = all)")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit JSON")
    fmt.add_argument("--csv", action="store_true", help="Emit CSV")
    p.add_argument("--reprice", action="store_true",
                   help="Price stored sessions whose cost is unknown, using pricing.overrides / the catalog")
    p.add_argument("--dry-run", action="store_true", help="With --reprice: report what would change, write nothing")
    p.set_defaults(func=cmd_costs)


def _parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"bad date {value!r}: expected YYYY-MM-DD") from exc


def resolve_window(args, *, now: Optional[float] = None) -> Tuple[float, float]:
    now = time.time() if now is None else now
    if getattr(args, "since", None):
        since = _parse_date(args.since).timestamp()
        until = (_parse_date(args.until) + timedelta(days=1)).timestamp() if getattr(args, "until", None) else now
        if until <= since:
            raise ValueError("--until must be on or after --since")
        return since, until
    if getattr(args, "until", None):
        until = (_parse_date(args.until) + timedelta(days=1)).timestamp()
        return until - int(args.days) * DAY, until
    return now - int(args.days) * DAY, now


def _money(value: float) -> str:
    return format_cost_label(Decimal(str(value)))


def _row_dict(row: ReportRow, key_columns: List[str]) -> dict:
    return {
        "period": row.period, "keys": {c: row.keys[c] for c in key_columns},
        "cost_usd": round(row.cost_usd, 6), "status": row.status, "sessions": row.sessions,
        "api_calls": row.api_calls, "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
        "cache_read_tokens": row.cache_read_tokens, "cache_write_tokens": row.cache_write_tokens,
        "unpriced_tokens": row.unpriced_tokens,
    }


def format_json(report: Report) -> str:
    return json.dumps({
        "window": {"since": report.since, "until": report.until,
                   "since_iso": datetime.fromtimestamp(report.since, tz=timezone.utc).isoformat(),
                   "until_iso": datetime.fromtimestamp(report.until, tz=timezone.utc).isoformat()},
        "by": report.by, "bucket": report.bucket,
        "rows": [_row_dict(r, report.key_columns) for r in report.rows],
        "total": _row_dict(report.total, []),
        "double_counted_usd": round(report.double_counted_usd, 6),
    }, indent=2)


_METRIC_COLUMNS = ["cost_usd", "status", "sessions", "api_calls", "input_tokens", "output_tokens",
                   "cache_read_tokens", "cache_write_tokens", "unpriced_tokens"]


def format_csv(report: Report) -> str:
    buf = io.StringIO()
    columns = (["period"] if report.bucket != "none" else []) + report.key_columns + _METRIC_COLUMNS
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for row in report.rows:
        d = _row_dict(row, report.key_columns)
        flat = {**d["keys"], **{c: d[c] for c in _METRIC_COLUMNS}}
        if report.bucket != "none":
            flat["period"] = row.period
        writer.writerow(flat)
    return buf.getvalue()


def format_table(report: Report) -> str:
    show_unpriced = report.total.unpriced_tokens > 0
    headers = (["period"] if report.bucket != "none" else []) + report.key_columns + \
              ["cost", "status", "sessions", "calls", "in_tokens", "out_tokens"] + \
              (["unpriced_tokens"] if show_unpriced else [])

    def cells(row: ReportRow, label: Optional[str] = None) -> List[str]:
        keys = [label] + [""] * (len(report.key_columns) - 1) if label else [row.keys[c] for c in report.key_columns]
        out = ([row.period or ""] if report.bucket != "none" else []) + keys + [
            _money(row.cost_usd), row.status, str(row.sessions), str(row.api_calls),
            f"{row.input_tokens:,}", f"{row.output_tokens:,}"]
        if show_unpriced:
            out.append(f"{row.unpriced_tokens:,}")
        return out

    body = [cells(r) for r in report.rows]
    total_label = "TOTAL" if report.key_columns else ""
    total_cells = cells(report.total, label=total_label) if report.key_columns else cells(report.total)
    widths = [max(len(h), *(len(r[i]) for r in body + [total_cells])) for i, h in enumerate(headers)]
    numeric_from = len(headers) - (7 if show_unpriced else 6)

    def line(vals: List[str]) -> str:
        return "  ".join(v.rjust(widths[i]) if i >= numeric_from else v.ljust(widths[i]) for i, v in enumerate(vals))

    lines = [line(headers), line(["-" * w for w in widths])]
    lines += [line(r) for r in body]
    lines.append(line(["-" * w for w in widths]))
    lines.append(line(total_cells))
    if report.double_counted_usd > 0:
        lines.append(f"\nNote: {_money(report.double_counted_usd)} of the rows above was counted more than once "
                     "(cron jobs delivering to several channels). TOTAL is true spend.")
    if show_unpriced:
        lines.append("\n" + _HINT)
    return "\n".join(lines)


def _window_line(since: float, until: float) -> str:
    fmt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"Window: {fmt(since)} → {fmt(until)}"


def run_costs(args, db) -> int:
    try:
        since, until = resolve_window(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    if getattr(args, "reprice", False):
        result = reprice(db, since=since, until=until, dry_run=bool(getattr(args, "dry_run", False)))
        prefix = "Dry run — would price" if result.dry_run else "Priced"
        print(_window_line(since, until))
        print(f"{prefix} {result.usage_rows_priced} usage rows across {result.sessions_updated} sessions "
              f"({result.sessions_priced_from_summary} legacy sessions without usage rows), "
              f"adding {_money(result.added_usd)}.")
        if result.skipped_unknown:
            print(f"Skipped {result.skipped_unknown} rows whose model still has no pricing entry.")
        return 0

    sessions = attribute_sessions(db, since=since, until=until, platform=getattr(args, "platform", None))
    report = aggregate(sessions, by=args.by, bucket=args.bucket, since=since, until=until, top=args.top)
    if args.json:
        print(format_json(report))
        return 0
    if args.csv:
        print(format_csv(report), end="")
        return 0
    print(_window_line(since, until))
    if not sessions:
        print("No sessions with usage in this window.")
        print(_HINT)
        return 0
    print(format_table(report))
    return 0
```

- [ ] **Step 4: Wire it into `hermes_cli/main.py`**

Import (next to the insights import, ~line 481):

```python
from hermes_cli.subcommands.costs import build_costs_parser
```

Handler (immediately after `cmd_insights`, ~line 12931):

```python
def cmd_costs(args):
    """Fork: spend attributed to Slack channels and cron jobs (agent/cost_attribution.py)."""
    db = None
    try:
        from hermes_state import SessionDB
        from hermes_cli.subcommands.costs import run_costs

        db = SessionDB(read_only=not getattr(args, "reprice", False))
        return run_costs(args, db)
    except Exception as e:
        print(f"Error generating cost report: {e}")
        return 1
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
```

Registration (right after `build_insights_parser(subparsers, cmd_insights=cmd_insights)`, ~line 14586):

```python
    build_costs_parser(subparsers, cmd_costs=cmd_costs)
```

Add `"costs",` after `"insights",` in the ordered list near line 10927, and after `"insights",` inside `_BUILTIN_SUBCOMMANDS` near line 12367. Check how `main()` treats a handler's integer return (grep `args.func(args)` in `main.py`); if it does `sys.exit(rc)` already, nothing more is needed, otherwise mirror what `cmd_insights` does.

- [ ] **Step 5: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/hermes_cli/test_costs_cli.py`
Expected: all pass

Then a smoke run against the real parser: `.venv/bin/hermes costs --help` should print the new usage; `.venv/bin/hermes costs --days 1` against the local store prints either a table or "No sessions with usage in this window."

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/subcommands/costs.py hermes_cli/main.py tests/hermes_cli/test_costs_cli.py
git commit -m "feat(cli): hermes costs — spend by channel / cron job / both, buckets, json/csv, --reprice

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Docs and fork inventory guard

**Files:**
- Modify: `tests/test_fork_feature_inventory.py` (the `WIRING` list, after the `pdf_pages` entry)
- Modify: `CLAUDE.md` (fork-specific section, after "Cron delivery format")
- Modify: `docs/superpowers/specs/2026-09-17-cost-attribution-design.md` (one wording fix)

**Interfaces:** none new.

- [ ] **Step 1: Add inventory needles**

Insert into `WIRING`:

```python
    # ── Cost attribution ──────────────────────────────────────────────────
    ("hermes costs", "agent/cost_attribution.py", "def attribute_sessions", "lineage query"),
    ("hermes costs", "agent/cost_attribution.py", "def reprice", "reprice pass"),
    ("hermes costs", "hermes_cli/subcommands/costs.py", "def build_costs_parser", "parser"),
    ("hermes costs", "hermes_cli/main.py", "build_costs_parser(subparsers, cmd_costs=cmd_costs)", "registered in main"),
    ("pricing overrides", "agent/usage_pricing.py", "pricing_override_for(model_name, route)", "override consulted first"),
```

Run: `scripts/run_tests.sh tests/test_fork_feature_inventory.py`
Expected: all pass

- [ ] **Step 2: Fix the spec's reconciliation wording**

`SessionDB.usage_totals` sums **root rows only** (`parent_session_id IS NULL`), while the report rolls descendants into their root. In the spec's "Attribution model" section, replace the sentence beginning "Totals in the `job` and `both` views always reconcile with `SessionDB.usage_totals`" with:

> Totals in the `job` and `both` views equal the sum of `COALESCE(actual_cost_usd, estimated_cost_usd)` over every session row (roots and descendants) whose root started in the window — a superset of `SessionDB.usage_totals`, which ignores child sessions.

And in "Testing", replace "`job` and `both` totals equal `usage_totals`" with "`job` and `both` totals equal the direct SUM over all rows in the window".

- [ ] **Step 3: CLAUDE.md entry**

Add after the "Cron delivery format" subsection:

```markdown
### Cost attribution — `hermes costs` + `pricing.overrides`

Upstream records tokens and cost per session (`sessions`, `session_model_usage`) but
only reports by model/platform. The fork adds
[agent/cost_attribution.py](agent/cost_attribution.py), a read-side query layer that
walks every session to its `parent_session_id` root and derives two keys at query
time — the **cron job** from the `cron_<job>_<ts>` root id, and the **channel** from
`origin_json` (`parent_chat_id` else `chat_id`; DMs key as `dm:<user>`); a cron job
delivering to a channel carries both, via `cron.scheduler._resolve_delivery_targets`.
No schema change. `hermes costs --days 30 --by channel|job|both|model|user
--bucket day|week|month [--json|--csv]`
([hermes_cli/subcommands/costs.py](hermes_cli/subcommands/costs.py)). The `channel`
view double-counts multi-target jobs on purpose (footer shows the overlap); `job` and
`both` partition true spend. Every row carries a status (`actual`/`estimated`/
`partial`/`unpriced`) plus `unpriced_tokens`, so a missing price is never a silent $0.
**The VM's `gpt-5.4-mini` is not in upstream's pricing catalog**, so set per-million
rates under a top-level `pricing.overrides:` block (`{input, output, cache_read,
cache_write}`); `get_pricing_entry` consults it before the catalog (`cost_source =
user_override`). `hermes costs --reprice [--dry-run]` prices stored rows whose
`cost_status` is unknown; already-priced rows are never rewritten. Design:
[docs/superpowers/specs/2026-09-17-cost-attribution-design.md](docs/superpowers/specs/2026-09-17-cost-attribution-design.md).
```

- [ ] **Step 4: Run the touched packages once more**

Run, one at a time:
- `scripts/run_tests.sh tests/agent/test_cost_attribution.py tests/agent/test_usage_pricing_overrides.py tests/agent/test_usage_pricing.py tests/agent/test_insights.py`
- `scripts/run_tests.sh tests/hermes_cli/test_costs_cli.py tests/test_fork_feature_inventory.py`

Expected: all pass. Then `ruff check agent/cost_attribution.py hermes_cli/subcommands/costs.py` clean.

- [ ] **Step 5: Commit**

```bash
git add -f docs/superpowers/specs/2026-09-17-cost-attribution-design.md
git add CLAUDE.md tests/test_fork_feature_inventory.py
git commit -m "docs(costs): CLAUDE.md entry, fork inventory guard, spec reconciliation wording

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

(`docs/superpowers/*` is gitignored; the specs are force-added, as every earlier spec was.)

---

## Deployment (after merge, not part of the plan's tasks)

1. Push `main`, fast-forward `~/ea-hermes` on the VM, restart the gateway (routine procedure).
2. Add to the VM's `config.yaml`, with the rates from OpenAI's pricing page at deploy time:
   ```yaml
   pricing:
     overrides:
       gpt-5.4-mini: {input: <rate>, output: <rate>, cache_read: <rate>}
   ```
3. `hermes costs --reprice --dry-run --days 365`, check the counts, then without `--dry-run`.
4. `hermes costs --days 30 --by both` is the first real report.
