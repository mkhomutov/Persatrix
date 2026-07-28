"""RFC 0037 §E — the §D gate's projection-selection branch (v0.3.12 PR 6).

When the :class:`~agents.persona_runtime.injection_gate.TurnInjectionGate`
withholds an entry, the §D contract says: inject the best declassification
projection of that entry with level ``≤ L`` instead, and only withhold
entirely when none exists.  This module is that branch, applied to the
two **episode-backed** tiers (``episodic`` + ``channel_history``) — the
only tiers whose entries have a Phase-2 projection producer (the RFC 0020
close-consolidation call writes ``entry_tier='episode'`` rows keyed by
interaction id; the ``fact`` / ``note`` producer is the RFC 0027
reflection pass, proposed, not shipped, so wiring those tiers here would
be dead surface).

Selection: per withheld episode, the projection with the **highest level
still in** ``injectable_levels(acting)`` — rule (b) floors an unknown
acting level to ``public``, and a corrupted stored projection level falls
out of the SQL IN-set (rule (c)).  Rule-(c) *entry* casualties (an
unparseable stored ``protection_level``) also reach this branch: the
projection row carries its own valid level, so serving it at ``≤ L`` is
classification-safe regardless of how the entry's own label was
corrupted — strictly better than the blunt withhold, disclosing nothing
the projection's level does not admit.

The replacement is the original :class:`~agents.memory.episode_types
.Episode` with ``summary`` swapped for the projection text and
``protection_level`` for the projection's own level, so the renderers
(recency tag, duration prefix, budget admission, MQ-11 provenance) treat
it like any other candidate and the §G manifest labels what actually
reached the prompt.  Order is preserved via the gate's per-tier decision
record: a projected entry re-enters exactly where the withheld original
stood in the relevance ranking.

Failure posture: any storage error degrades to the Phase-1 blunt
withhold (the safe direction — never fail open, never fail the turn).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from ..memory.projections import ENTRY_TIER_EPISODE, projections_for
from .classification import entry_rank_or_withhold, injectable_levels

if TYPE_CHECKING:
    from ..memory.episodic import EpisodicMemory
    from ..memory.episodic_queries import Episode
    from .injection_gate import TurnInjectionGate

logger = logging.getLogger(__name__)

__all__ = ["apply_episode_projections"]

#: The episode-backed injection tiers this branch serves, in the order the
#: caller filters them.
_EPISODE_TIERS: tuple[str, str] = ("channel_history", "episodic")


def _level_rank(level: str) -> int:
    """Sort key for picking the highest admissible projection.

    Every candidate already passed the ``injectable_levels`` IN-set, so
    the rank is always defined; ``-1`` keeps the key total if a future
    caller relaxes that (an unknown level then sorts below ``public``,
    never above a real one).
    """
    rank = entry_rank_or_withhold(level)
    return -1 if rank is None else rank


async def apply_episode_projections(
    gate: TurnInjectionGate,
    episodic: EpisodicMemory,
    *,
    channel_history: list[Episode],
    episodic_entries: list[Episode],
) -> tuple[list[Episode], list[Episode]]:
    """Serve §E projections for the gate's withheld episodes.

    Takes (and, when nothing projects, returns unchanged) the two
    gate-admitted episode lists; on a projection hit, returns the tier
    rebuilt in candidate order with the declassified replacement in the
    withheld original's position.  Each served projection is registered
    on the gate (:meth:`TurnInjectionGate.record_projection`) so the
    manifest labels it at the projection's own level.
    """
    withheld_ids = {
        interaction_id
        for tier in _EPISODE_TIERS
        for entry, admitted in gate.decisions(tier)
        if not admitted
        and (interaction_id := getattr(entry, "interaction_id", None))
    }
    if not withheld_ids:
        return channel_history, episodic_entries
    try:
        available = await projections_for(
            episodic,
            entry_tier=ENTRY_TIER_EPISODE,
            entry_ids=sorted(withheld_ids),
            levels=injectable_levels(gate.acting),
        )
    except Exception:
        logger.warning(
            "§E projection lookup failed; withheld entries stay withheld",
            exc_info=True,
        )
        return channel_history, episodic_entries
    if not available:
        return channel_history, episodic_entries

    def rebuild(tier: str, admitted_now: list[Episode]) -> list[Episode]:
        out: list[Episode] = []
        served = False
        for entry, admitted in gate.decisions(tier):
            episode = cast("Episode", entry)
            if admitted:
                out.append(episode)
                continue
            candidates = available.get(episode.interaction_id or "")
            if not candidates:
                continue
            level, text = max(candidates, key=lambda lt: _level_rank(lt[0]))
            out.append(replace(episode, summary=text, protection_level=level))
            gate.record_projection(tier=tier, entry_id=episode.id, level=level)
            served = True
        return out if served else admitted_now

    return (
        rebuild("channel_history", channel_history),
        rebuild("episodic", episodic_entries),
    )
