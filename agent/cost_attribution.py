"""Attribute Hermes spend to Slack channels and cron jobs (fork-only).

Pure query layer over ``SessionDB``: nothing here writes except ``reprice``.
Attribution is derived at query time from columns that already exist —
``parent_session_id`` (lineage), the ``cron_<job>_<ts>`` session id (job),
and ``origin_json`` (channel). See
docs/superpowers/specs/2026-09-17-cost-attribution-design.md.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
    all_actual: bool = True


@dataclass
class AttributedSession:
    session_id: str
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


# Guard against a pathological (or cyclic-looking) parent chain walking forever.
# 1000 is far above any real lineage; hitting it is logged, never silent.
_MAX_LINEAGE_DEPTH = 1000

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
     WHERE l.depth < {max_depth}
)
SELECT l.root_id, l.orphan, l.depth,
       s.id, s.source, s.user_id, s.chat_id, s.chat_type, s.origin_json, s.display_name,
       s.started_at, s.api_call_count,
       COALESCE(s.input_tokens, 0) AS input_tokens,
       COALESCE(s.output_tokens, 0) AS output_tokens,
       COALESCE(s.cache_read_tokens, 0) AS cache_read_tokens,
       COALESCE(s.cache_write_tokens, 0) AS cache_write_tokens,
       s.estimated_cost_usd, s.actual_cost_usd, s.cost_status,
       r.source AS root_source, r.user_id AS root_user,
       r.chat_id AS root_chat_id, r.chat_type AS root_chat_type,
       r.origin_json AS root_origin_json, r.display_name AS root_display_name
  FROM lineage l
  JOIN sessions s ON s.id = l.id
  JOIN sessions r ON r.id = l.root_id
 WHERE s.started_at >= ? AND s.started_at < ?
 ORDER BY s.started_at, s.id
"""

_MODEL_USAGE_SQL = """
SELECT u.session_id, u.model, u.billing_provider,
       u.api_call_count, u.input_tokens, u.output_tokens,
       u.cache_read_tokens, u.cache_write_tokens,
       u.estimated_cost_usd, u.actual_cost_usd, u.cost_status, u.task
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


def _model_usage_cost(row) -> float:
    # Unlike sessions.actual_cost_usd (nullable), session_model_usage's
    # actual_cost_usd is NOT NULL DEFAULT 0 (_record_model_usage always
    # writes float(actual_cost_usd or 0.0)) — so "not None" can't tell
    # "recorded actual" from "never priced actual". cost_status is the
    # only reliable signal here.
    if row["cost_status"] == "actual":
        return float(row["actual_cost_usd"] or 0.0)
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


def _memoize_targets(target_resolver: TargetResolver) -> TargetResolver:
    """Resolve each cron job's delivery targets once per report.

    Every cron *run* is its own root, so a report over a busy store visits
    thousands of cron roots for a dozen jobs — and
    ``cron.scheduler._resolve_delivery_targets`` costs up to a second per
    call (it consults the gateway config and profile listing). Keyed on the
    job id; jobs without an id fall back to object identity.
    """
    cache: Dict[Any, List[dict]] = {}

    def resolve(job: dict) -> List[dict]:
        key = str(job.get("id")) if isinstance(job, dict) and job.get("id") else id(job)
        if key not in cache:
            cache[key] = target_resolver(job)
        return cache[key]

    return resolve


@dataclass(frozen=True)
class _RootKeys:
    """The attribution keys a root lends to every session in its lineage."""
    source: str
    user_id: Optional[str]
    job_id: Optional[str]
    job_name: Optional[str]
    channels: Tuple[ChannelKey, ...]


def _root_keys(row, names: Dict[Tuple[str, str], str],
               job_resolver: JobResolver, target_resolver: TargetResolver) -> _RootKeys:
    root_id = row["root_id"]
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
    return _RootKeys(source=row["root_source"] or "", user_id=row["root_user"],
                     job_id=job_id, job_name=job_name, channels=tuple(channels))


def _set_busy_timeout(conn) -> None:
    """Wait for the live gateway's writer instead of failing instantly.

    ``SessionDB(read_only=True)`` opens with ``timeout=1.0`` and no
    busy_timeout; a store that is not in WAL mode therefore raises "database
    is locked" the moment the gateway writes. Harmless under WAL.
    """
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:  # a pragma must never be the reason a report fails
        logger.debug("cost_attribution: busy_timeout pragma failed", exc_info=True)


def attribute_sessions(
    db,
    *,
    since: float,
    until: float,
    platform: Optional[str] = None,
    job_resolver: Optional[JobResolver] = None,
    target_resolver: Optional[TargetResolver] = None,
) -> List[AttributedSession]:
    """One row per session that ran in [since, until), keyed on its root.

    Each contributing session is windowed and bucketed on its **own**
    ``started_at``; the root supplies the keys (job, channel, user, platform).
    A months-old Slack channel root therefore still reports this week's spend,
    and long-lived roots do not smear their descendants' cost onto the day
    they started.
    """
    job_resolver = job_resolver or _default_job_resolver()
    target_resolver = _memoize_targets(target_resolver or _default_target_resolver)

    attributed: Dict[str, AttributedSession] = {}
    root_keys: Dict[str, _RootKeys] = {}
    deepest = (-1, "")
    with db._read_ctx() as conn:
        _set_busy_timeout(conn)
        names = _channel_name_index(conn)
        rows = conn.execute(_LINEAGE_SQL.format(max_depth=_MAX_LINEAGE_DEPTH),
                            (since, until)).fetchall()
        for row in rows:
            root_id = row["root_id"]
            keys = root_keys.get(root_id)
            if keys is None:
                keys = root_keys[root_id] = _root_keys(row, names, job_resolver, target_resolver)
            depth = int(row["depth"] or 0)
            if depth > deepest[0]:
                deepest = (depth, root_id)
            agg = AttributedSession(
                session_id=row["id"], root_id=root_id, source=keys.source,
                started_at=float(row["started_at"]), user_id=keys.user_id,
                job_id=keys.job_id, job_name=keys.job_name, channels=list(keys.channels),
            )
            attributed[row["id"]] = agg

            tokens = int(row["input_tokens"]) + int(row["output_tokens"])
            if tokens == 0 and not row["api_call_count"]:
                # Bare gateway row: nothing consumed in the main loop. The row
                # still exists to receive this session's auxiliary usage below,
                # but it is not counted as a session and is dropped if nothing
                # lands on it.
                continue
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

        if deepest[0] >= _MAX_LINEAGE_DEPTH - 1:
            logger.warning(
                "cost_attribution: session lineage under root %s reached the depth cap "
                "(%d); descendants below it are not attributed",
                deepest[1], _MAX_LINEAGE_DEPTH,
            )

        session_ids = list(attributed)
        for chunk_start in range(0, len(session_ids), 500):
            chunk = session_ids[chunk_start:chunk_start + 500]
            sql = _MODEL_USAGE_SQL.format(placeholders=",".join("?" * len(chunk)))
            for urow in conn.execute(sql, chunk):
                # A session's usage rows belong to that session's row only —
                # never to the root's — or the root would count them twice.
                agg = attributed[urow["session_id"]]
                key = (urow["model"] or "", urow["billing_provider"] or "")
                mu = agg.models.setdefault(key, ModelUsage(model=key[0], provider=key[1]))
                mu.api_calls += int(urow["api_call_count"] or 0)
                mu.input_tokens += int(urow["input_tokens"] or 0)
                mu.output_tokens += int(urow["output_tokens"] or 0)
                mu.cache_read_tokens += int(urow["cache_read_tokens"] or 0)
                mu.cache_write_tokens += int(urow["cache_write_tokens"] or 0)
                row_tokens = int(urow["input_tokens"] or 0) + int(urow["output_tokens"] or 0)
                row_priced = (urow["cost_status"] in PRICED_STATUSES
                              or (urow["estimated_cost_usd"] or 0) > 0)
                if row_priced:
                    mu.cost_usd += _model_usage_cost(urow)
                    if urow["cost_status"] != "actual":
                        mu.all_actual = False
                else:
                    mu.priced = False
                if (urow["task"] or "") != "":
                    # Auxiliary call (vision, compression, title_generation, ...):
                    # record_auxiliary_usage keeps these OUT of the sessions summary
                    # row, so fold them into their own session here — every view then
                    # shares one cost basis (main loop + aux), matching what the model
                    # view sums from the same table.
                    agg.api_calls += int(urow["api_call_count"] or 0)
                    agg.input_tokens += int(urow["input_tokens"] or 0)
                    agg.output_tokens += int(urow["output_tokens"] or 0)
                    agg.cache_read_tokens += int(urow["cache_read_tokens"] or 0)
                    agg.cache_write_tokens += int(urow["cache_write_tokens"] or 0)
                    if row_priced:
                        agg.cost_usd += _model_usage_cost(urow)
                        if urow["cost_status"] != "actual":
                            agg.all_actual = False
                    else:
                        agg.unpriced_tokens += row_tokens

    # A bare row with no usage rows of its own consumed nothing: drop it.
    result = [r for r in attributed.values() if r.sessions > 0 or r.models]
    if platform:
        wanted = platform.strip().lower()
        result = [r for r in result
                  if (r.source or "").lower() == wanted or any(c.platform.lower() == wanted for c in r.channels)]
    result.sort(key=lambda r: (r.started_at, r.session_id))
    return result


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
    rows_omitted: int = 0   # rows `top` cut; TOTAL still covers them


def _session_contribution(s: AttributedSession) -> dict:
    return dict(cost_usd=s.cost_usd, sessions=s.sessions, api_calls=s.api_calls,
                input_tokens=s.input_tokens, output_tokens=s.output_tokens,
                cache_read_tokens=s.cache_read_tokens, cache_write_tokens=s.cache_write_tokens,
                priced_tokens=s.priced_tokens, unpriced_tokens=s.unpriced_tokens, all_actual=s.all_actual)


def _model_contribution(m: ModelUsage) -> dict:
    tokens = m.input_tokens + m.output_tokens
    return dict(cost_usd=m.cost_usd, sessions=0, api_calls=m.api_calls,  # a session can span several models, so per-model session counts are meaningless
                input_tokens=m.input_tokens, output_tokens=m.output_tokens,
                cache_read_tokens=m.cache_read_tokens, cache_write_tokens=m.cache_write_tokens,
                priced_tokens=tokens if m.priced else 0, unpriced_tokens=0 if m.priced else tokens,
                all_actual=m.all_actual)


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
    rows_omitted = 0
    if top and top > 0:
        rows_omitted = max(0, len(ordered) - top)
        ordered = ordered[:top]
    return Report(by=by, bucket=bucket, since=since, until=until, key_columns=key_columns,
                  rows=ordered, total=total, double_counted_usd=double_counted,
                  rows_omitted=rows_omitted)


@dataclass
class RepriceResult:
    usage_rows_priced: int = 0
    usage_rows_recomputed: int = 0
    sessions_updated: int = 0
    sessions_priced_from_summary: int = 0
    skipped_unknown: int = 0
    added_usd: float = 0.0
    dry_run: bool = False


# Two kinds of row are eligible:
#   1. never priced (status unknown/null and no cost stored), and
#   2. priced from the operator's own pricing.overrides (cost_source =
#      'user_override'), whose price is fully determined by that table, so
#      recomputing it from the stored tokens is exact and idempotent.
# (2) exists because session_model_usage rows are UPSERT-accumulated with
# cost_status = COALESCE(excluded.cost_status, cost_status): a session that was
# alive when the overrides landed keeps all its earlier unpriced tokens on the
# row but flips to 'estimated' carrying only the first priced call's cost.
# Without the recompute those rows are mis-priced forever. Rows priced from
# provider data or the catalog are never touched.
_UNPRICED_USAGE_SQL = """
SELECT u.rowid AS rid, u.session_id, u.model, u.billing_provider, u.billing_base_url, u.task,
       u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,
       u.estimated_cost_usd, u.actual_cost_usd, u.cost_status, u.cost_source,
       s.cost_status AS session_cost_status
  FROM session_model_usage u JOIN sessions s ON s.id = u.session_id
 WHERE s.started_at >= ? AND s.started_at < ?
   AND ( ( (u.cost_status IS NULL OR u.cost_status = 'unknown')
           AND COALESCE(u.actual_cost_usd, 0) = 0
           AND COALESCE(u.estimated_cost_usd, 0) = 0 )
         OR (u.cost_source = 'user_override' AND COALESCE(u.cost_status, '') != 'actual') )
   AND (u.input_tokens + u.output_tokens) > 0
 ORDER BY u.session_id
"""

_UNPRICED_LEGACY_SESSIONS_SQL = """
SELECT s.id, s.model, s.billing_provider, s.billing_base_url, s.cost_source,
       COALESCE(s.estimated_cost_usd, 0) AS estimated_cost_usd,
       COALESCE(s.input_tokens, 0) AS input_tokens, COALESCE(s.output_tokens, 0) AS output_tokens,
       COALESCE(s.cache_read_tokens, 0) AS cache_read_tokens, COALESCE(s.cache_write_tokens, 0) AS cache_write_tokens,
       COALESCE(s.reasoning_tokens, 0) AS reasoning_tokens
  FROM sessions s
 WHERE s.started_at >= ? AND s.started_at < ?
   AND COALESCE(s.cost_status, '') != 'actual'
   AND ( ( (s.cost_status IS NULL OR s.cost_status = 'unknown')
           AND COALESCE(s.actual_cost_usd, 0) = 0 )
         OR s.cost_source = 'user_override' )
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
    """Price stored rows whose cost is unknown, and recompute override-priced rows.

    Uses the current ``pricing.overrides`` / catalog. Rows priced from provider
    data or the catalog are never touched — only unpriced rows and rows whose
    ``cost_source`` is ``user_override`` (see ``_UNPRICED_USAGE_SQL``).
    """
    result = RepriceResult(dry_run=dry_run)
    with db._read_ctx() as conn:
        _set_busy_timeout(conn)
        usage_rows = [dict(r) for r in conn.execute(_UNPRICED_USAGE_SQL, (since, until))]
        legacy_rows = [dict(r) for r in conn.execute(_UNPRICED_LEGACY_SESSIONS_SQL, (since, until))]

    usage_updates: Dict[str, List[tuple]] = {}   # session_id -> [(rid, amount, source, version, task)]
    actual_sessions = {row["session_id"] for row in usage_rows if row["session_cost_status"] == "actual"}
    for row in usage_rows:
        est = _estimate(row)
        if est is None:
            result.skipped_unknown += 1
            continue
        amount, source, version = est
        usage_updates.setdefault(row["session_id"], []).append((row["rid"], amount, source, version, row["task"]))
        if row["cost_source"] == "user_override":
            result.usage_rows_recomputed += 1
        else:
            result.usage_rows_priced += 1
        # Only the delta: a recomputed row already contributes its old cost.
        result.added_usd += amount - _model_usage_cost(row)

    legacy_updates: List[tuple] = []             # (session_id, amount, source, version)
    for row in legacy_rows:
        est = _estimate(row)
        if est is None:
            result.skipped_unknown += 1
            continue
        amount, source, version = est
        legacy_updates.append((row["id"], amount, source, version))
        result.sessions_priced_from_summary += 1
        result.added_usd += amount - float(row["estimated_cost_usd"] or 0.0)

    # A session whose summary is provider-'actual' keeps that summary: its usage
    # rows are still priced, but rewriting the row would downgrade real money to
    # an estimate.
    result.sessions_updated = len({sid for sid, ups in usage_updates.items()
                                   if sid not in actual_sessions and any(u[4] == "" for u in ups)}) \
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
            if main_loop and session_id not in actual_sessions:
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
