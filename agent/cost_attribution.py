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
                    mu.cost_usd += _model_usage_cost(urow)
                else:
                    mu.priced = False

    result = [r for r in roots.values() if r.sessions > 0]
    if platform:
        wanted = platform.strip().lower()
        result = [r for r in result
                  if (r.source or "").lower() == wanted or any(c.platform.lower() == wanted for c in r.channels)]
    result.sort(key=lambda r: (r.started_at, r.root_id))
    return result
