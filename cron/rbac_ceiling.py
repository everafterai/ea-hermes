"""Per-creator RBAC toolset ceiling for cron jobs.

Cron execution runs with no platform identity in session contextvars, so the
RBAC execution backstop (gateway/tool_access.denial_for_current_tool) cannot
gate a cron-spawned agent. A job's ``enabled_toolsets`` would otherwise be the
sole determinant of the agent's capabilities with no check against the role of
the user who created it — letting a non-admin who can author a job escalate to
``terminal`` and a host shell.

This module caps a cron job's resolved ``enabled_toolsets`` to the toolset grant
of its creator's CURRENT RBAC role. The creator is read from the automation
ownership registry (``cron:<job_id>``); the role is resolved fresh each run, so
a later demotion shrinks the ceiling.

Operator decisions (see
docs/superpowers/specs/2026-06-30-cron-rbac-toolset-ceiling-design.md):
  * Jobs with NO resolvable creator role — no owner record, or an owner who is
    now roleless / has an undefined role — run UNCAPPED (legacy/ownerless jobs
    must keep working). Surfaced via the data-access audit when elevated.
  * The cap is the primary cron control, not a backstop: it fails OPEN on any
    internal error, mirroring gateway/tool_access.filter_enabled_toolsets.
"""

from __future__ import annotations

import logging
from typing import FrozenSet, List, Optional

logger = logging.getLogger(__name__)


def apply_cron_toolset_ceiling(
    resolved: Optional[List[str]], grant: Optional[FrozenSet[str]]
) -> Optional[List[str]]:
    """Intersect a cron job's resolved toolset list with the creator's grant.

    ``resolved`` is the output of
    cron.scheduler._resolve_cron_enabled_toolsets: a list of toolset names, or
    None meaning "AIAgent loads the full default set". ``grant`` comes from
    :func:`cron_owner_grant`. Returns the capped, sorted list, or ``resolved``
    unchanged when no ceiling applies (grant is None, or the role grants
    everything via "*").
    """
    try:
        if grant is None or "*" in grant:
            return resolved
        from gateway.tool_access import FLOOR_TOOLSETS, _granted

        if resolved is not None:
            universe = frozenset(resolved)
        else:
            from toolsets import get_all_toolsets

            universe = frozenset(get_all_toolsets())
        return sorted(
            t for t in universe if _granted(grant, t) or t in FLOOR_TOOLSETS
        )
    except Exception as err:  # pragma: no cover - defensive, fail-open
        logger.debug("apply_cron_toolset_ceiling failed (fail-open): %s", err)
        return resolved
