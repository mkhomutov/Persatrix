"""Decision-recording metric registrations (RFC 0028 PR plan PR 3).

Split out of :mod:`agents.observability.metrics` like the sibling
``_metrics_*`` modules. The counters are module-owned (the
``_metrics_confidentiality`` precedent), and :func:`register` re-creates
them on every ``_Instruments`` construction so they track the live meter.

Both move only while recording is on (plan E-10):

* ``agent.decision.records`` counts the decisions recorded, by checkpoint
  and the class chosen;
* ``agent.decision.sink_errors`` counts a sink that failed to take one —
  the decision log or the checkpoint span — which never fails the turn.

Every attribute is a closed vocabulary: the agent, a ``Checkpoint``, an
``ActionClass`` (or ``none`` for a directive's verdict), and ``log`` or
``span``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Meter

    from .metrics import _Instruments

_records: Counter | None = None
_sink_errors: Counter | None = None


def record_decision(*, agent_id: str, checkpoint: str, action_class: str) -> None:
    """Count one recorded decision. A no-op until :func:`register` has run,
    and best-effort after it: a metric must never fail the turn."""
    counter = _records
    if counter is None:
        return
    with contextlib.suppress(Exception):
        counter.add(1, attributes={
            "agent.id": agent_id, "checkpoint": checkpoint, "action_class": action_class,
        })


def record_sink_error(*, agent_id: str, sink: str) -> None:
    """Count one sink that failed to take a decision; best-effort, like
    :func:`record_decision`."""
    counter = _sink_errors
    if counter is None:
        return
    with contextlib.suppress(Exception):
        counter.add(1, attributes={"agent.id": agent_id, "sink": sink})


def register(inst: _Instruments, meter: Meter) -> None:
    """Register the module-owned decision counters."""
    global _records, _sink_errors
    _records = meter.create_counter(
        name="agent.decision.records",
        unit="{record}",
        description=(
            "Decisions recorded at a decision checkpoint while recording is "
            "on (RFC 0028). Attributes: agent.id, checkpoint, action_class."
        ),
    )
    _sink_errors = meter.create_counter(
        name="agent.decision.sink_errors",
        unit="{error}",
        description=(
            "A decision the decision log or the checkpoint span failed to "
            "take; the turn goes on. Attributes: agent.id, sink (log|span)."
        ),
    )
