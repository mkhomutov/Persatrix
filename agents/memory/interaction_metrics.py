"""Interaction lifecycle counter emission (RFC 0020 §C).

Extracted from :mod:`agents.memory.interactions` when the v0.3.15
residuals PR 3 tracker re-key (`(principal, speaker, scope)` —
ISSUE-0123 / ISSUE-0131) pushed that module past the 500-line cap
(``scripts/checks/file_size.py --strict``) — the same module-split
precedent as :mod:`agents.memory.scopes` and
:mod:`agents.memory.interaction_types`.  The tracker module re-exports
nothing from here; these helpers are internal to
:class:`~agents.memory.interaction_tracker.InteractionTracker`.

Counter-shape note (ISSUE-0123 part 3, stated not discovered): since the
tracker keys records ``(principal, speaker, scope)``, a room-wide close
fans over every open record in the scope and ``_emit_closed`` fires once
per RECORD — so ``agent.interactions.closed`` and its per-reason
subtotals increment by N on a room close where they used to increment
by 1.  Dashboards summing these counters are counting records, not room
events.
"""

from __future__ import annotations

from ..observability.metrics import current_agent_id, try_get_instruments
from .boundary_detectors import (
    REASON_CATCHUP_COMPLETE,
    REASON_COST,
    REASON_IDLE_GAP,
    REASON_MAX_TURNS,
    REASON_SHUTDOWN,
    REASON_STRUCTURAL,
    REASON_TOPIC_SHIFT,
    CloseReason,
)

__all__ = ["_emit_closed", "_emit_opened"]


def _emit_opened() -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    inst.interactions_opened.add(1, {"agent_id": current_agent_id()})


# Reason → ``_Instruments`` attribute holding the per-reason subtotal
# counter.  Keep this table in lockstep with :data:`CloseReason` and
# the counter registrations in
# :class:`agents.observability.metrics._Instruments` — adding a new
# reason without an entry here drops its subtotal silently (the prior
# ``if/elif`` chain had the same property; the dict shape just makes
# the breakage table-shaped instead of statement-shaped, and a future
# enum migration is a one-line swap).
_REASON_COUNTER_ATTR: dict[CloseReason, str] = {
    REASON_STRUCTURAL: "interactions_closed_by_structural",
    REASON_IDLE_GAP: "interactions_closed_by_idle_gap",
    REASON_MAX_TURNS: "interactions_closed_by_max_turns",
    REASON_TOPIC_SHIFT: "interactions_closed_by_topic_shift",
    REASON_SHUTDOWN: "interactions_closed_by_shutdown",
    REASON_COST: "interactions_closed_by_cost",
    REASON_CATCHUP_COMPLETE: "interactions_closed_by_catchup_complete",
}


def _emit_closed(reason: CloseReason) -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    attrs = {"agent_id": current_agent_id(), "reason": reason}
    inst.interactions_closed.add(1, attrs)
    counter_attr = _REASON_COUNTER_ATTR.get(reason)
    if counter_attr is not None:
        getattr(inst, counter_attr).add(1, attrs)
