"""``hermes costs`` — spend attributed to Slack channels and cron jobs (fork-only).

Parser + formatters only; the query layer is ``agent.cost_attribution``.
Handler injected (``cmd_costs``) to avoid importing ``main``.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, List, Optional, Tuple

from agent.cost_attribution import BUCKETS, VIEWS, Report, ReportRow, aggregate, attribute_sessions, reprice
from agent.usage_pricing import format_cost_label

DAY = 86400.0
_HINT = ("Hint: models missing from the pricing catalog show as unpriced. Add per-million rates under\n"
         "      pricing.overrides in config.yaml, then run `hermes costs --reprice` to price stored history.")
_SCHEMA_HINT = ("Error: the session store's schema is out of date ({err}).\n"
                "The report opens the store read-only and never migrates it. Open it once with a write\n"
                "command to migrate — `hermes costs --reprice --dry-run` does that without changing any\n"
                "cost — then run the report again.")
_LOCKED_HINT = "Error: the session store is locked (the gateway is writing). Retry in a moment."


def _is_schema_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("no such column" in msg or "no such table" in msg)


def _store_path() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home() / "state.db")


def store_error_message(exc: BaseException) -> Optional[str]:
    """One-line explanation for the two store-level sqlite errors, else None.

    Shared with ``cmd_costs``, which opens the store and so hits these before
    ``run_costs`` ever runs.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return None
    msg = str(exc).lower()
    if "database is locked" in msg:
        return _LOCKED_HINT
    if "unable to open database file" in msg:
        return (f"Error: no session store at {_store_path()} "
                "(nothing recorded yet, or wrong --profile).")
    return None


def build_costs_parser(subparsers, *, cmd_costs: Callable) -> None:
    """Attach the ``costs`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "costs",
        help="Show spend attributed to Slack channels and cron jobs",
        description="Attribute session cost to channels and automations over a time window.",
    )
    window = p.add_mutually_exclusive_group()
    # No default: a report falls back to 30 days, --reprice to the whole store.
    window.add_argument("--days", type=int, default=None,
                        help="Window ending now (default: 30; with --reprice: the whole store)")
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


DEFAULT_DAYS = 30


def resolve_window(args, *, now: Optional[float] = None) -> Tuple[float, float]:
    now = time.time() if now is None else now
    days = getattr(args, "days", None)
    if getattr(args, "since", None):
        since = _parse_date(args.since).timestamp()
        until = (_parse_date(args.until) + timedelta(days=1)).timestamp() if getattr(args, "until", None) else now
        if until <= since:
            raise ValueError("--until must be on or after --since")
        return since, until
    if getattr(args, "until", None):
        until = (_parse_date(args.until) + timedelta(days=1)).timestamp()
        return until - int(days if days is not None else DEFAULT_DAYS) * DAY, until
    if days is None and getattr(args, "reprice", False):
        # Repricing is a repair pass: with no window asked for, cover the whole
        # store, or the history nobody thought to name stays mis-priced.
        return 0.0, now
    return now - int(days if days is not None else DEFAULT_DAYS) * DAY, now


def _money(value: float) -> str:
    return format_cost_label(Decimal(str(value)))


def _row_dict(row: ReportRow, key_columns: List[str], *, by: str = "") -> dict:
    return {
        "period": row.period, "keys": {c: row.keys[c] for c in key_columns},
        "cost_usd": round(row.cost_usd, 6), "status": row.status,
        # A session spans several models, so the model view has no session count
        # to give: null, not a misleading 0. CSV writes it as an empty cell.
        "sessions": None if by == "model" else row.sessions,
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
        "rows": [_row_dict(r, report.key_columns, by=report.by) for r in report.rows],
        "total": _row_dict(report.total, [], by=report.by),
        "double_counted_usd": round(report.double_counted_usd, 6),
        "rows_omitted": report.rows_omitted,
    }, indent=2)


_METRIC_COLUMNS = ["cost_usd", "status", "sessions", "api_calls", "input_tokens", "output_tokens",
                   "cache_read_tokens", "cache_write_tokens", "unpriced_tokens"]


def format_csv(report: Report) -> str:
    buf = io.StringIO()
    columns = (["period"] if report.bucket != "none" else []) + report.key_columns + _METRIC_COLUMNS
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for row in report.rows:
        d = _row_dict(row, report.key_columns, by=report.by)
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

    # Per-model session counts are meaningless (a session spans several models).
    sessions_cell = (lambda row: "-") if report.by == "model" else (lambda row: str(row.sessions))

    def cells(row: ReportRow, label: Optional[str] = None) -> List[str]:
        keys = [label] + [""] * (len(report.key_columns) - 1) if label else [row.keys[c] for c in report.key_columns]
        out = ([row.period or ""] if report.bucket != "none" else []) + keys + [
            _money(row.cost_usd), row.status, sessions_cell(row), str(row.api_calls),
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
    if report.rows_omitted > 0:
        lines.append(f"\n({report.rows_omitted} more rows not shown; TOTAL covers all. Raise --top to see them.)")
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
        try:
            result = reprice(db, since=since, until=until, dry_run=bool(getattr(args, "dry_run", False)))
        except sqlite3.OperationalError as exc:
            message = store_error_message(exc)
            if message is None:
                raise
            print(message)
            return 1
        prefix = "Dry run — would price" if result.dry_run else "Priced"
        print(_window_line(since, until))
        print(f"{prefix} {result.usage_rows_priced} usage rows across {result.sessions_updated} sessions "
              f"({result.sessions_priced_from_summary} legacy sessions without usage rows), "
              f"and recomputed {result.usage_rows_recomputed} rows priced from pricing.overrides, "
              f"adding {_money(result.added_usd)}.")
        if result.skipped_unknown:
            print(f"Skipped {result.skipped_unknown} rows whose model still has no pricing entry.")
        return 0

    try:
        sessions = attribute_sessions(db, since=since, until=until, platform=getattr(args, "platform", None))
        report = aggregate(sessions, by=args.by, bucket=args.bucket, since=since, until=until, top=args.top)
    except sqlite3.OperationalError as exc:
        message = store_error_message(exc)
        if message is not None:
            print(message)
            return 1
        if not _is_schema_error(exc):
            raise
        print(_SCHEMA_HINT.format(err=exc))
        return 1
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
