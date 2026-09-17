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
