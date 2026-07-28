"""RFC 0037 §G (v0.3.12 PR 7) tripwire-rate metric registration.

Split out of :mod:`agents.observability.metrics` (at the 500-line review
cap) like the sibling ``_metrics_*`` modules; the instrument is
module-owned (the ``_metrics_salience`` deliberation precedent) so
``metrics.py`` gains no class annotation.  :func:`register` re-creates it
on every ``_Instruments`` construction so it always tracks the live
meter.

``channel.confidentiality.tripwire_hits`` counts §G tripwire hits — a
normalized verbatim span of a §D-withheld entry observed in an outgoing
channel message.  Because §D keeps withheld text out of the prompt, a
non-zero rate indicates a *bug* (a mis-stamped entry, a §E projection
that copied source text verbatim, or a missed injection path) — exactly
the defect class RFC 0037 most needs surfaced in early operation, and
the telemetry the §G span-threshold tuning (OQ 5) waits on.  Both
attributes are closed, bounded vocabularies: ``tier`` is the gate's
four-tier set and ``protection_level`` is the §A lattice plus the
rule-(c) sentinel ``unknown``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Meter

    from .metrics import _Instruments

_tripwire: Counter | None = None


def record_tripwire_hit(*, tier: str, protection_level: str) -> None:
    """Record one §G tripwire hit.  A no-op until :func:`register` has run
    (metrics unconfigured) and best-effort after it — the emit rides the
    publish path *after* the message was authored, so a metric-export
    hiccup must never propagate (the ``record_deliberation`` contract)."""
    counter = _tripwire
    if counter is None:
        return
    with contextlib.suppress(Exception):
        counter.add(
            1, attributes={"tier": tier, "protection_level": protection_level},
        )


def register(inst: _Instruments, meter: Meter) -> None:
    """Register the module-owned §G tripwire counter."""
    global _tripwire
    _tripwire = meter.create_counter(
        name="channel.confidentiality.tripwire_hits",
        unit="{hit}",
        description=(
            "RFC 0037 §G leak-tripwire hits: a normalized verbatim span of "
            "a §D-withheld memory entry observed in an outgoing channel "
            "message. Observability only — the message is not blocked. "
            "Attributes: tier (channel_history|facts|episodic|notes), "
            "protection_level (the §A lattice, or 'unknown' for a rule-(c) "
            "corrupted stored label). Non-zero indicates a stamping/"
            "projection/injection bug, not normal operation."
        ),
    )
