"""L1 cross-room episodic recall — SHADOW mode (RFC 0049 Phase 1 PR 3).

The `RFC 0049 L1 amendment
<../../docs/rfcs/0049-amendment-l1-cross-room-availability.md>`_ converts
the RFC 0031 §D session filter, as the episodic default, from hard wall
to **room-first ranking** — same-room episodes boosted, other-room
episodes admissible but demoted, every cross-room candidate behind the
RFC 0037 §D gate.  This module is the amendment's **shadow**
implementation, the L1 sibling of :mod:`.facts_shadow`: each
channel-anchored turn re-runs the live episodic recall through the
room-first-RANKED mode (:func:`~agents.memory.episodic_room_ranked
.recall_room_ranked`, same query/limit/``min_score`` as the live call),
takes the cross-room DELTA (ranked rows the live room-walled recall did
not return), §D-gates every candidate at the turn's acting
classification, and records ONE structured log trace — nothing enters
the live prompt.  The RFC 0044 harness captures the traces
(``evaluators/persona_driver.py``); the PR 4 measurement gate reads them
to decide the shadow → live flip.

Scope invariants:

* Only the **session** axis widens; ``epoch`` and ``principal`` remain
  strict-equality SQL walls on every branch of the widened read.
* The widened read is **side-effect-free** (no ``access_count`` bump) —
  the live composite score reads ``access_count``, so a reinforcing
  shadow would perturb live ranking on later turns and shift the landed
  RFC 0044 goldens off their cassettes (see ``episodic_room_ranked``).
* Only **channel-anchored** turns (:data:`CHANNEL_ACTING_EVENT_TYPES`)
  run the pass: the tick-shaped class floors to the rule-(b) ``public``
  acting level and is the RFC 0017 §F cheap-idle path — the PR 4
  measurement targets real channel turns, and idle ticks keep costing
  zero DB round-trips.

F-3 posture (the widened-read security pin): like ``facts_shadow``,
this module deliberately reads across sessions but is NOT a
prompt-context path — nothing here touches :class:`WorkingMemory`, the
RFC 0017 budget, the §G manifest, or any reinforcement write; the sole
output is a log record.  ``test_episodes_shadow.py`` pins the
no-prompt-leak property; ``test_session_recall_default_path.py``
documents the carve-out.

Log-egress bound: the trace names each candidate's ``episode_id`` /
``protection_level`` / provenance (``session_id``,
``source_channel_id``) / widened-rank position, but never the episode
**summary** (or context/outcome/tags) — the process log is its own
egress surface.  The PR 4 measurement joins ``episode_id`` back against
the store when it needs content.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from ..memory.episodic import DEFAULT_EPISODIC_MIN_SCORE
from ..memory.episodic_room_ranked import recall_room_ranked
from .episodic_section import EPISODIC_RECALL_LIMIT
from .facts_shadow import (
    CROSS_ROOM_MODES,
    CROSS_ROOM_SHADOW,
)
from .injection_gate import (
    CHANNEL_ACTING_EVENT_TYPES,
    TurnInjectionGate,
    acting_classification_for_event,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from ..memory.episodic import EpisodicMemory
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_EPISODIC_CROSS_ROOM",
    "SHADOW_LOGGER_NAME",
    "SHADOW_TRACE_ATTR",
    "emit_episodes_shadow",
    "resolve_episodic_cross_room",
]

#: The logger name the RFC 0044 harness attaches its capture handler to.
SHADOW_LOGGER_NAME: Final[str] = __name__

#: Attribute on each shadow log record carrying the structured trace
#: payload — distinct from the facts attr so a handler listening to both
#: loggers can read each record's payload off its own key.
SHADOW_TRACE_ATTR: Final[str] = "episodes_shadow"

#: ``memory.episodic.cross_room`` shares the facts knob's closed mode
#: vocabulary (``off`` | ``shadow``; ``"live"`` rejected until the PR 4
#: promotion) and its shadow-first shipped posture.
DEFAULT_EPISODIC_CROSS_ROOM: Final[str] = CROSS_ROOM_SHADOW


def resolve_episodic_cross_room(config: dict) -> str:
    """Resolve ``memory.episodic.cross_room`` from a persona config.

    The exact twin of
    :func:`~agents.persona_runtime.facts_shadow.resolve_facts_cross_room`
    on the ``memory.episodic`` block: absent/``None`` → the shadow
    default; anything outside the closed mode set — most likely an early
    ``"live"`` — raises at agent construction rather than silently
    degrading a requested live widening to shadow.
    """
    episodic_cfg = (config.get("memory") or {}).get("episodic") or {}
    raw = episodic_cfg.get("cross_room")
    if raw is None:
        return DEFAULT_EPISODIC_CROSS_ROOM
    if not isinstance(raw, str) or raw not in CROSS_ROOM_MODES:
        raise ValueError(
            f"memory.episodic.cross_room must be one of "
            f"{sorted(CROSS_ROOM_MODES)}, got {raw!r}",
        )
    return raw


async def emit_episodes_shadow(
    episodic_memory: EpisodicMemory | None,
    event: AgentEvent,
    *,
    query: str,
    live_episode_ids: Collection[str],
    agent_id: str,
    mode: str = DEFAULT_EPISODIC_CROSS_ROOM,
) -> None:
    """Compute and record the turn's L1 cross-room shadow trace.

    One structured INFO record per channel-anchored turn with a
    non-empty cross-room delta; quiet turns (empty delta, tick-shaped
    events, ``mode="off"``, missing store) emit nothing, so single-room
    deployments see zero log volume.  Runs OUTSIDE the live tier
    pipeline and never raises — a shadow failure degrades to a WARNING,
    honouring ``_inject_memory_context``'s "never fail the event"
    contract.

    The delta preserves the widened read's boosted-rank ORDER, and each
    candidate carries its ``rank`` (0-based position in the widened
    result, gate-withheld rows included) — "this row would have been
    the prompt's #N episodic line", the displacement signal the PR 4
    measurement compares against the live top-N.  The §D gate uses a
    dedicated :class:`TurnInjectionGate` whose aggregated log emission
    is NOT fired (a shadow row never had a prompt to be withheld from);
    withhold counts ride the trace split by cause — ``withheld`` (clean
    above-rank) vs ``unknown_label`` (rule (c)) — the same two fields
    the PR 4 consumer reads off the facts traces.
    """
    if mode != CROSS_ROOM_SHADOW or episodic_memory is None:
        return
    if event.event_type not in CHANNEL_ACTING_EVENT_TYPES:
        return
    try:
        # Same query / limit / min_score as the live episodic recall in
        # ``_inject_memory_context`` — the shadow-vs-live comparison is
        # like-for-like by construction (both read the shared constants).
        widened = await recall_room_ranked(
            episodic_memory, query,
            limit=EPISODIC_RECALL_LIMIT,
            min_score=DEFAULT_EPISODIC_MIN_SCORE,
        )
        live_ids = set(live_episode_ids)
        delta = [
            (rank, ep)
            for rank, ep in enumerate(widened)
            if ep.id not in live_ids
        ]
        if not delta:
            return
        acting = acting_classification_for_event(event)
        gate = TurnInjectionGate(acting=acting, agent_id=agent_id)
        candidates = [
            (rank, ep)
            for rank, ep in delta
            if gate.admit(
                tier="episodic", entry_id=ep.id,
                protection_level=ep.protection_level,
                source_channel_id=ep.source_channel_id,
            )
        ]
        trace = {
            "tier": "episodic",
            "agent_id": agent_id,
            "acting": acting,
            "candidates": [
                {
                    "episode_id": ep.id,
                    "rank": rank,
                    "protection_level": ep.protection_level,
                    "session_id": ep.session_id,
                    "source_channel_id": ep.source_channel_id,
                }
                for rank, ep in candidates
            ],
            "withheld": gate.withheld_count,
            "unknown_label": gate.unknown_label_count,
        }
        logger.info(
            "Agent %s: L1 cross-room shadow — %d episode(s) would rank "
            "into the prompt at acting=%r (%d withheld above rank, "
            "%d unknown-label); live prompt unchanged",
            agent_id, len(candidates), acting,
            trace["withheld"], trace["unknown_label"],
            extra={SHADOW_TRACE_ATTR: trace},
        )
    except Exception:
        logger.warning(
            "Agent %s: L1 cross-room shadow pass failed; skipping",
            agent_id, exc_info=True,
        )
