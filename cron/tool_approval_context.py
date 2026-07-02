"""Per-run cron context for the per-tool approval gate.

The scheduler resolves the job owner's toolset grant and the job's
``unattended_approved_tools`` at run start and stashes them here so the
approval gate (running deep in model_tools) can authorize headless tool calls
without threading the job dict through the dispatcher.
"""
from __future__ import annotations

import contextvars
from typing import Optional, Tuple

_owner_grant: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "cron_tool_owner_grant", default=None)
_acked_tools: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "cron_tool_acked", default=None)
_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "cron_tool_active", default=False)


def set_cron_tool_context(*, owner_grant, acked_tools):
    t1 = _owner_grant.set(frozenset(owner_grant) if owner_grant is not None else None)
    t2 = _acked_tools.set(frozenset(acked_tools or ()))
    t3 = _active.set(True)
    return (t1, t2, t3)


def clear_cron_tool_context(token) -> None:
    t1, t2, t3 = token
    _owner_grant.reset(t1)
    _acked_tools.reset(t2)
    _active.reset(t3)


def get_cron_tool_context() -> Tuple[Optional[frozenset], frozenset]:
    return _owner_grant.get(), (_acked_tools.get() or frozenset())


def in_cron_run() -> bool:
    return bool(_active.get())
