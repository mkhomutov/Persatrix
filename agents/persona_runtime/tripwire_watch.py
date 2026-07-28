"""RFC 0037 §G (v0.3.12 PR 7) — building the per-turn tripwire watch.

The persona-runtime half of Phase 3: after
:meth:`~agents.persona_runtime.memory_context._MemoryContextMixin
._inject_memory_context` has run every tier through the §D gate, the
WITHHELD candidates become a hash-only
:class:`~agents.confidentiality_tripwire.TripwireWatch` stamped onto the
turn's event metadata; ``DispatchContext.for_event`` lifts it
structurally to the ``ActionExecutor``, where the §G check runs (see
:mod:`agents.confidentiality_tripwire` for why the watch is the withheld
set and why the executor is the check site).

The watch is rebuilt from the gate's per-tier decision record rather
than a new gate tally: the record already holds every candidate object
in arrival order, and re-deriving rule (c) through the same
``entry_rank_or_withhold`` helper the gate used means the two can never
disagree on which withholds were unknown-label casualties.  A §E
projection served in place of a withheld entry changes nothing here —
the *verbatim* entry was withheld (that is the §D guarantee), so its
fingerprint stays on the watch, which is exactly how a projection that
copied source text verbatim gets caught.

Import direction: this module (persona_runtime) imports the executor-side
:mod:`agents.confidentiality_tripwire`, never the reverse — the executor
entry points must not grow a hard dep on the persona subpackage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from ..confidentiality_tripwire import (
    TripwireWatch,
    TripwireWatchEntry,
    span_hashes,
    stamp_tripwire_watch,
)
from .classification import entry_rank_or_withhold

if TYPE_CHECKING:
    from ..persona_types import AgentEvent
    from .injection_gate import TurnInjectionGate

logger = logging.getLogger(__name__)

__all__ = ["TIER_CONTENT_ATTRS", "build_tripwire_watch", "stamp_turn_tripwire_watch"]

#: Per-tier content attribute — the natural-language text the prompt
#: would have rendered, which is what a verbatim leak would copy.  The
#: key set doubles as the watch's tier walk and is drift-pinned to the
#: gate's ``_MANIFEST_TIERS`` (``test_tripwire_watch.py``); a future
#: fifth gated tier must be consciously added to both.
TIER_CONTENT_ATTRS: Final[dict[str, str]] = {
    "channel_history": "summary",
    "facts": "object",
    "episodic": "summary",
    "notes": "content",
}

#: Rule-(c) sentinel: a corrupted stored label is treated above-``secret``
#: by the gate, and the watch reports it as ``unknown`` — the raw label
#: never rides (unbounded, possibly content-bearing).
_UNKNOWN_LEVEL: Final[str] = "unknown"


def build_tripwire_watch(gate: TurnInjectionGate) -> TripwireWatch | None:
    """The turn's watch: every withheld candidate with a span fingerprint.

    Returns ``None`` when nothing withheld is watchable — the common case
    (no operator has classified above ``internal``, or every withheld
    entry is below the span threshold), so the turn stamps nothing and
    the executor no-ops.  Admitted entries are deliberately absent: their
    level is ≤ the acting level = the §B-guarded publish target, so they
    can never satisfy §G's above-target condition.
    """
    entries: list[TripwireWatchEntry] = []
    for tier, content_attr in TIER_CONTENT_ATTRS.items():
        for entry, admitted in gate.decisions(tier):
            if admitted:
                continue
            content = getattr(entry, content_attr, None)
            if not isinstance(content, str):
                continue
            hashes = span_hashes(content)
            if not hashes:
                continue
            level = getattr(entry, "protection_level", None)
            known = entry_rank_or_withhold(level) is not None
            entries.append(TripwireWatchEntry(
                tier=tier,
                entry_id=str(getattr(entry, "fact_id", None)
                             or getattr(entry, "id", "")),
                protection_level=level if known else _UNKNOWN_LEVEL,  # type: ignore[arg-type]
                span_hashes=hashes,
            ))
    if not entries:
        return None
    return TripwireWatch(acting=gate.acting, entries=tuple(entries))


def stamp_turn_tripwire_watch(
    event: AgentEvent, gate: TurnInjectionGate,
) -> None:
    """Build the watch off ``gate`` and stamp it onto ``event``'s metadata.

    Never fails: this runs inside ``_inject_memory_context``'s never-fail
    contract, and §G is observability — a poisoned decision record
    degrades to an unwatched turn (logged), never a failed injection.
    """
    try:
        stamp_tripwire_watch(event.metadata, build_tripwire_watch(gate))
    except Exception:
        logger.warning(
            "Agent %s: §G tripwire watch build failed; turn unwatched",
            getattr(gate, "_agent_id", "?"), exc_info=True,
        )
