import json
import time

import pytest

from agent.cost_attribution import (
    ChannelKey,
    attribute_sessions,
    channel_from_origin,
    cron_job_id_from_session_id,
    is_thread_origin,
)
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
        # parent_session_id has an FK to sessions(id); a genuinely dangling
        # reference (parent row pruned/never persisted) can only be seeded
        # with FK enforcement off for this one insert.
        db._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            _seed(db, "orphan", source="subagent", started_at=NOW - DAY, parent="gone", cost=0.1)
        finally:
            db._conn.execute("PRAGMA foreign_keys=ON")
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
