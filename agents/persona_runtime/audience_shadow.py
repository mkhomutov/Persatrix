"""ISSUE-0132 (v0.3.16 PR A2) — the audience decision, as a trace.

The audience check ships in ``shadow``: it records what it *would* have
withheld and changes no prompt (:mod:`.audience`).  A recording that
nobody can read is not a measurement, so this module is the readable
half — one structured INFO record per turn, on the same
``SHADOW_LOGGER_NAME`` / ``SHADOW_TRACE_ATTR`` contract the RFC 0049
shadow passes use, so the RFC 0044 harness threads it into the report
artifact beside them and ``evaluators/shadow_measurement.py`` renders
the verdict scope lock 1 gates the flip on.

Two properties this module owes, both inherited rather than invented:

* **Quiet turns say nothing.** A turn whose candidates produced no
  verdict — a single-room deployment, a turn with only ``public``
  entries — emits no record at all, so enabling the shadow costs no log
  volume where there is nothing to measure.
* **The log is its own egress surface.** The trace names each entry's
  tier, id, protection level, source room and verdict; it never carries
  the entry's **content**.  Dumping the protected text into the process
  log would undo at the log exactly what the gate enforces at the
  prompt.  The measurement joins ids back against the store when it
  needs content — it never has.

Emitted in ``live`` too, not only in ``shadow``: an operator who has
flipped the knob needs the per-entry record more, not less, and
``withheld`` then reports what the turn actually withheld rather than
zero.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from .audience import AUDIENCE_OFF, AudienceVerdict

if TYPE_CHECKING:
    from .audience import TurnAudience
    from .injection_gate import TurnInjectionGate

logger = logging.getLogger(__name__)

__all__ = [
    "SHADOW_LOGGER_NAME",
    "SHADOW_TRACE_ATTR",
    "emit_audience_shadow",
]

#: The logger name the RFC 0044 harness attaches its capture handler to.
SHADOW_LOGGER_NAME: Final[str] = __name__

#: Attribute on each shadow log record carrying the structured payload.
SHADOW_TRACE_ATTR: Final[str] = "audience_shadow"

#: The ``tier`` key the measurement partitions on — the RFC 0049 traces
#: use ``"facts"`` / ``"episodic"``; this is the third partition.
AUDIENCE_TIER: Final[str] = "audience"


def emit_audience_shadow(
    gate: TurnInjectionGate,
    *,
    agent_id: str,
    mode: str,
    audience: TurnAudience | None,
) -> None:
    """Record this turn's audience verdicts, or nothing at all.

    Never raises — ``_inject_memory_context``'s never-fail contract
    extends to its observability: a poisoned decision record degrades to
    an untraced turn (logged), never a failed injection.
    """
    if mode == AUDIENCE_OFF:
        return
    try:
        records = gate.audience_records
        if not records:
            return
        verdicts = {v.value: 0 for v in AudienceVerdict}
        for record in records:
            verdicts[record.verdict.value] += 1
        trace = {
            "tier": AUDIENCE_TIER,
            "agent_id": agent_id,
            "acting": gate.acting,
            "acting_channel_id": (
                audience.acting_channel_id if audience is not None else None
            ),
            "mode": mode,
            "candidates": [
                {
                    "tier": r.tier,
                    "entry_id": r.entry_id,
                    "protection_level": r.protection_level,
                    "source_channel_id": r.source_channel_id,
                    "verdict": r.verdict.value,
                }
                for r in records
            ],
            "verdicts": verdicts,
            # ``withheld`` is what the audience check actually withheld
            # (0 in shadow, by definition); ``unknown_label`` is carried
            # as a constant 0 so this tier's summary is shape-compatible
            # with the RFC 0049 tiers' — a §D rule-(c) casualty never
            # reaches the audience check, so the field is not merely
            # unset here, it is structurally zero.
            "withheld": gate.audience_withheld_count,
            "unknown_label": 0,
            "source_rooms": audience.source_rooms if audience is not None else 0,
            "fetches": audience.fetches if audience is not None else 0,
        }
        logger.info(
            "Agent %s: audience egress (%s) — %d entr%s judged at "
            "acting=%r: %d admit, %d disjoint, %d unknown "
            "(%d fetch-failed, %d no-provenance); %d withheld",
            agent_id, mode, len(records),
            "y" if len(records) == 1 else "ies", gate.acting,
            verdicts[AudienceVerdict.ADMIT.value],
            verdicts[AudienceVerdict.WITHHOLD_DISJOINT.value],
            verdicts[AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED.value]
            + verdicts[AudienceVerdict.WITHHOLD_UNKNOWN_NO_PROVENANCE.value],
            verdicts[AudienceVerdict.WITHHOLD_UNKNOWN_FETCH_FAILED.value],
            verdicts[AudienceVerdict.WITHHOLD_UNKNOWN_NO_PROVENANCE.value],
            gate.audience_withheld_count,
            extra={SHADOW_TRACE_ATTR: trace},
        )
    except Exception:
        logger.warning(
            "Agent %s: audience shadow trace failed; turn untraced",
            agent_id, exc_info=True,
        )
