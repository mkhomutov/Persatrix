"""Shared-memory-pool metric registrations (RFC 0008 PR plan PR 4).

Split out of :mod:`agents.observability.metrics` when RFC 0028 PR 3 needed a
line in it at its 500-line cap, as the sibling ``_metrics_*`` modules were.
The counters are assigned to the parent :class:`_Instruments`, so
``agents.memory.shared_pool`` still reaches them as ``inst.shared_pool_reads``.

They are recorded by the pool's ACL gates. ``denied`` carries an
``operation`` attribute (read|write|publish), so operators can tell a
trust-boundary breach from a misrouted writer without joining traces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register the four ``agent.shared_pool.*`` counters on ``inst``."""
    inst.shared_pool_reads = meter.create_counter(
        name="agent.shared_pool.reads",
        unit="{call}",
        description=(
            "Reads served by a SharedMemoryPool (post-ACL).  Attributes: "
            "pool, agent.id, result.count."
        ),
    )
    inst.shared_pool_writes = meter.create_counter(
        name="agent.shared_pool.writes",
        unit="{entry}",
        description=(
            "Entries written to a SharedMemoryPool (post-ACL + provenance "
            "injection).  Attributes: pool, agent.id."
        ),
    )
    inst.shared_pool_denied = meter.create_counter(
        name="agent.shared_pool.denied",
        unit="{call}",
        description=(
            "ACL or sensitivity denials raised by SharedMemoryPool.  "
            "Attributes: pool, agent.id, operation."
        ),
    )
    inst.shared_pool_evictions = meter.create_counter(
        name="agent.shared_pool.evictions",
        unit="{entry}",
        description=(
            "Entries evicted by the FIFO ``max_entries`` cap on a "
            "SharedMemoryPool.  Attributes: pool."
        ),
    )
