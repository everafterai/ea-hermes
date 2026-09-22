import json
import logging
import time

import pytest

import hermes_cli.config as hermes_config
from agent import cost_attribution
from agent.cost_attribution import (
    AttributedSession, ChannelKey, ModelUsage,
    aggregate, attribute_sessions,
    channel_from_origin,
    cron_job_id_from_session_id,
    is_thread_origin, period_label, reprice,
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
        assert r.session_id == "s1"
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
        # One row per contributing session; the root supplies the keys.
        assert [r.session_id for r in rows] == ["root", "child", "grandchild"]
        assert all(r.root_id == "root" for r in rows)
        assert all(r.channels[0].chat_id == "C123" for r in rows)
        assert all(r.sessions == 1 for r in rows)
        assert sum(r.cost_usd for r in rows) == pytest.approx(1.0)
        assert sum(r.input_tokens for r in rows) == 3000
        rep = aggregate(rows, by="channel", **WINDOW)
        assert len(rep.rows) == 1
        assert rep.rows[0].sessions == 3
        assert rep.rows[0].cost_usd == pytest.approx(1.0)

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
        sessions = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        rows = {r.session_id: r for r in sessions}
        # Per contributing session each is whole; the mix shows up once they aggregate.
        assert rows["root"].status == "estimated"
        assert rows["child"].status == "unpriced" and rows["child"].unpriced_tokens == 1000
        assert rows["lonely"].status == "unpriced"
        rep = aggregate(sessions, by="both", **WINDOW)
        by_channel = {r.keys["channel"]: r for r in rep.rows}
        assert by_channel["slack:issues"].status == "partial"
        assert by_channel["slack:issues"].unpriced_tokens == 1000
        assert by_channel["slack:C9"].status == "unpriced"

    def test_status_actual_only_when_every_priced_session_is_actual(self, db):
        _seed(db, "a", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=0.1, actual=0.12, status="actual")
        _seed(db, "b", started_at=NOW - DAY, chat_id="C2", chat_type="channel", cost=0.1, status="estimated")
        rows = {r.root_id: r for r in attribute_sessions(db, **WINDOW, job_resolver=_jobs(),
                                                          target_resolver=_targets_from_job)}
        assert rows["a"].status == "actual" and rows["a"].cost_usd == pytest.approx(0.12)
        assert rows["b"].status == "estimated"

    def test_window_filters_on_contributing_session(self, db):
        _seed(db, "old", started_at=NOW - 40 * DAY, chat_id="C123", chat_type="channel",
              origin=SLACK_ISSUES, cost=1.0)
        _seed(db, "new", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=1.0)
        # A long-lived root ages out of the window; its recent child must not
        # take the root's spend with it (and must keep the root's keys).
        _seed(db, "recent_child", source="subagent", started_at=NOW - DAY, parent="old", cost=99.0)
        rows = {r.session_id: r for r in attribute_sessions(db, **WINDOW, job_resolver=_jobs(),
                                                            target_resolver=_targets_from_job)}
        assert set(rows) == {"new", "recent_child"}
        child = rows["recent_child"]
        assert child.root_id == "old"
        assert [c.label for c in child.channels] == ["slack:issues"]
        assert child.cost_usd == pytest.approx(99.0)
        assert child.started_at == pytest.approx(NOW - DAY)

    def test_buckets_follow_each_session_not_the_root(self, db):
        _seed(db, "root", started_at=NOW - 2 * DAY, chat_id="C123", chat_type="channel",
              origin=SLACK_ISSUES, cost=0.5)
        _seed(db, "child", source="subagent", started_at=NOW - DAY, parent="root", cost=0.25)
        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        rep = aggregate(rows, by="channel", bucket="day", **WINDOW)
        assert [(r.period, r.cost_usd) for r in rep.rows] == [
            (period_label(NOW - 2 * DAY, "day"), pytest.approx(0.5)),
            (period_label(NOW - DAY, "day"), pytest.approx(0.25)),
        ]

    def test_deep_lineage_is_capped_and_warns(self, db, monkeypatch, caplog):
        _seed(db, "d0", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=0.1)
        for i in range(1, 6):
            _seed(db, f"d{i}", source="subagent", started_at=NOW - DAY, parent=f"d{i - 1}", cost=0.1)
        monkeypatch.setattr(cost_attribution, "_MAX_LINEAGE_DEPTH", 3)
        with caplog.at_level(logging.WARNING, logger="agent.cost_attribution"):
            rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(), target_resolver=_targets_from_job)
        assert [r.session_id for r in rows] == ["d0", "d1", "d2", "d3"]
        assert "depth" in caplog.text and "d0" in caplog.text

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
        # Aux rows are now folded into the root session
        assert rows[0].cost_usd == pytest.approx(0.51)
        assert rows[0].input_tokens == 1010
        assert rows[0].api_calls == 2


    def test_delivery_targets_are_resolved_once_per_job_not_per_run(self, db):
        # Every cron run is its own root; on the VM that is thousands of roots for
        # a dozen jobs, and each target resolution costs up to a second.
        for stamp in ("20260901_080000", "20260902_080000", "20260903_080000"):
            _seed(db, f"cron_ab12_{stamp}", source="cron", started_at=NOW - DAY, cost=1.0)
        job = {"id": "ab12", "name": "Digest", "_targets": [{"platform": "slack", "chat_id": "C123"}]}
        calls = []

        def counting_targets(j):
            calls.append(j["id"])
            return _targets_from_job(j)

        rows = attribute_sessions(db, **WINDOW, job_resolver=_jobs(job), target_resolver=counting_targets)
        assert len(rows) == 3
        assert all(r.channels and r.channels[0].chat_id == "C123" for r in rows)
        assert calls == ["ab12"]


class TestDefaultResolvers:
    """The production resolvers, which every test above replaces with a fake."""

    JOB = {"id": "ab12", "name": "X", "deliver": "slack:C123",
           "origin": {"platform": "slack", "chat_id": "C123"}}

    def test_job_resolver_finds_a_real_job(self, monkeypatch):
        import cron.jobs

        monkeypatch.setattr(cron.jobs, "load_jobs", lambda *a, **k: [self.JOB])
        resolver = cost_attribution._default_job_resolver()
        assert resolver("ab12")["name"] == "X"
        assert resolver("nope") is None

    def test_job_resolver_survives_a_broken_jobs_file(self, monkeypatch):
        import cron.jobs

        def boom(*_a, **_k):
            raise RuntimeError("jobs.json is corrupt")

        monkeypatch.setattr(cron.jobs, "load_jobs", boom)
        assert cost_attribution._default_job_resolver()("ab12") is None

    def test_target_resolver_returns_the_delivery_channel(self):
        targets = cost_attribution._default_target_resolver(self.JOB)
        assert [(t["platform"], t["chat_id"]) for t in targets] == [("slack", "C123")]
        from_origin = cost_attribution._default_target_resolver(
            {**self.JOB, "deliver": "origin"})
        assert [(t["platform"], t["chat_id"]) for t in from_origin] == [("slack", "C123")]

    def test_target_resolver_survives_a_raising_scheduler(self, monkeypatch):
        import cron.scheduler

        def boom(*_a, **_k):
            raise RuntimeError("no gateway config")

        monkeypatch.setattr(cron.scheduler, "_resolve_delivery_targets", boom)
        assert cost_attribution._default_target_resolver(self.JOB) == []


def _row(root_id, *, cost, job=None, channels=(), started_at=NOW - DAY, user="U1",
         unpriced=0, all_actual=False, tokens=1000, models=None):
    return AttributedSession(
        session_id=root_id, root_id=root_id, source="slack", started_at=started_at, user_id=user,
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

    def test_model_view_status_actual_when_all_rows_actual(self):
        models = {("m", "p"): ModelUsage("m", "p", api_calls=1, input_tokens=10, cost_usd=0.1, all_actual=True)}
        rep = aggregate([_row("a", cost=0.1, models=models)], by="model", since=0, until=NOW)
        assert rep.rows[0].status == "actual"

    def test_unknown_bucket_raises(self):
        with pytest.raises(ValueError):
            aggregate([], by="job", bucket="fortnight", since=0, until=NOW)

    def test_job_and_model_views_share_one_total_with_aux_rows(self, db):
        _seed(db, "s1", started_at=NOW - DAY, chat_id="C1", chat_type="channel", cost=0.5)
        db.record_auxiliary_usage("s1", "vision", model="gpt-4o", billing_provider="openai",
                                  input_tokens=10, output_tokens=5, estimated_cost_usd=0.01)
        sessions = attribute_sessions(db, **WINDOW, job_resolver=lambda _j: None, target_resolver=lambda _j: [])
        by_job = aggregate(sessions, by="job", since=0, until=NOW)
        by_model = aggregate(sessions, by="model", since=0, until=NOW)
        assert by_job.total.cost_usd == pytest.approx(by_model.total.cost_usd) == pytest.approx(0.51)


@pytest.fixture()
def priced_mini(monkeypatch):
    cfg = {"pricing": {"overrides": {"gpt-5.4-mini": {"input": 1.0, "output": 2.0}}}}
    monkeypatch.setattr(hermes_config, "read_raw_config", lambda: cfg)
    monkeypatch.setattr(hermes_config, "read_raw_config_readonly", lambda: cfg)


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

    def test_leaves_priced_rows_of_models_without_an_override_alone(self, db, priced_mini):
        # gpt-4o is catalog-priced and has no override: nothing states a better
        # rate for it, so its stored estimate stands.
        _seed(db, "p1", started_at=NOW - DAY, chat_id="C1", chat_type="channel", model="gpt-4o",
              cost=0.5, status="estimated", input_tokens=1_000_000, output_tokens=0)
        result = reprice(db, **WINDOW)
        assert result.usage_rows_priced == 0 and result.sessions_updated == 0
        assert result.usage_rows_recomputed == 0 and result.skipped_unknown == 0
        assert _session_cost_row(db, "p1")["estimated_cost_usd"] == pytest.approx(0.5)

    def test_catalog_priced_rows_of_an_overridden_model_are_recomputed(self, db, priced_mini):
        # Stored at a stale catalog rate ($0.50 for 1M input); the operator has
        # since stated $1.00/M for gpt-5.4-mini, which governs the model's whole
        # history — the VM's gpt-5.6-terra snapshot was 25% above OpenAI's bill.
        _seed(db, "stale", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              cost=0.5, status="estimated", input_tokens=1_000_000, output_tokens=0)
        result = reprice(db, **WINDOW)
        assert result.usage_rows_recomputed == 1 and result.usage_rows_priced == 0
        assert result.sessions_updated == 1
        assert result.added_usd == pytest.approx(0.5)       # delta: 1.0 - 0.5
        row = _session_cost_row(db, "stale")
        assert row["estimated_cost_usd"] == pytest.approx(1.0)
        assert row["cost_source"] == "user_override"
        assert reprice(db, **WINDOW).added_usd == pytest.approx(0.0)

    def test_provider_actual_rows_are_never_recomputed(self, db, priced_mini):
        _seed(db, "real", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              cost=0.5, actual=0.42, status="actual", input_tokens=1_000_000, output_tokens=0)
        result = reprice(db, **WINDOW)
        assert result.usage_rows_recomputed == 0 and result.usage_rows_priced == 0
        row = _session_cost_row(db, "real")
        assert row["cost_status"] == "actual"
        assert db._conn.execute("SELECT actual_cost_usd FROM session_model_usage WHERE session_id='real'").fetchone()[0] == pytest.approx(0.42)

    def _straddle(self, db):
        """A session alive when pricing.overrides landed.

        10M unpriced tokens accumulate first; the next call is priced under the
        override and — because _record_model_usage UPSERTs with
        cost_status = COALESCE(excluded.cost_status, cost_status) — flips the
        whole row to 'estimated' carrying only that call's $0.001.
        """
        _seed(db, "straddle", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              cost=None, status="unknown", input_tokens=10_000_000, output_tokens=0)
        db.update_token_counts("straddle", input_tokens=1_000, output_tokens=0, model="gpt-5.4-mini",
                               billing_provider="openai", estimated_cost_usd=0.001,
                               cost_status="estimated", cost_source="user_override",
                               pricing_version="user-override", api_call_count=1)

    def test_straddled_override_row_is_recomputed_in_full(self, db, priced_mini):
        self._straddle(db)
        assert _session_cost_row(db, "straddle")["estimated_cost_usd"] == pytest.approx(0.001)
        result = reprice(db, **WINDOW)
        assert result.usage_rows_recomputed == 1
        assert result.usage_rows_priced == 0
        assert result.sessions_updated == 1
        assert result.added_usd == pytest.approx(10.0)      # delta only: 10.001 - 0.001
        row = _session_cost_row(db, "straddle")
        assert row["estimated_cost_usd"] == pytest.approx(10.001)
        assert row["cost_source"] == "user_override"

    def test_recompute_is_idempotent(self, db, priced_mini):
        self._straddle(db)
        reprice(db, **WINDOW)
        again = reprice(db, **WINDOW)
        assert again.usage_rows_recomputed == 1
        assert again.added_usd == pytest.approx(0.0)
        assert _session_cost_row(db, "straddle")["estimated_cost_usd"] == pytest.approx(10.001)

    def test_actual_session_summary_is_never_downgraded(self, db, priced_mini):
        # Provider-reported actual on the summary, override-priced usage row:
        # the usage row is repriced, the 'actual' summary is left alone.
        _seed(db, "act", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              input_tokens=1_000_000, output_tokens=0, cost=0.2, actual=0.2, status="actual")
        db._conn.execute("UPDATE session_model_usage SET cost_source = 'user_override', "
                         "cost_status = 'estimated' WHERE session_id = 'act'")
        db._conn.commit()
        result = reprice(db, **WINDOW)
        assert result.usage_rows_recomputed == 1
        assert result.sessions_updated == 0
        row = _session_cost_row(db, "act")
        assert row["cost_status"] == "actual"
        assert row["estimated_cost_usd"] == pytest.approx(0.2)
        usage = db._conn.execute(
            "SELECT estimated_cost_usd FROM session_model_usage WHERE session_id = 'act'").fetchone()
        assert usage["estimated_cost_usd"] == pytest.approx(1.0)

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

    def test_aux_rows_are_priced_but_never_enter_the_summary_row(self, db, priced_mini):
        # Main loop: 1M in / 0 out → $1.00 under the override. Aux vision call on the
        # same model, unpriced (estimated_cost_usd=None): 500k in / 0 out → $0.50.
        _seed(db, "mixed", started_at=NOW - DAY, chat_id="C1", chat_type="channel",
              cost=None, status="unknown", input_tokens=1_000_000, output_tokens=0)
        db.record_auxiliary_usage("mixed", "vision", model="gpt-5.4-mini", billing_provider="openai",
                                  input_tokens=500_000, output_tokens=0, estimated_cost_usd=None)
        result = reprice(db, **WINDOW)
        assert result.usage_rows_priced == 2
        assert result.sessions_updated == 1
        assert result.added_usd == pytest.approx(1.5)
        # Summary row re-summed from task='' rows only — the aux $0.50 must not leak in.
        row = _session_cost_row(db, "mixed")
        assert row["estimated_cost_usd"] == pytest.approx(1.0)
        assert row["cost_status"] == "estimated"
        aux = db._conn.execute(
            "SELECT estimated_cost_usd, cost_status FROM session_model_usage WHERE session_id = 'mixed' AND task = 'vision'"
        ).fetchone()
        assert aux["estimated_cost_usd"] == pytest.approx(0.5)
        assert aux["cost_status"] == "estimated"
        # The attribution rollup (Task 3 ruling: aux rows fold into the root) sees both.
        rows = attribute_sessions(db, **WINDOW, job_resolver=lambda _j: None, target_resolver=lambda _j: [])
        assert rows[0].cost_usd == pytest.approx(1.5)
        assert rows[0].status == "estimated"
