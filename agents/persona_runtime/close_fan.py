"""The room-wide close fans — many records closed by one event, each
persisted behind its own guard.

Split out of :mod:`agents.persona_runtime.close_path` (v0.3.16 PR D2,
the ISSUE-0143 debt sweep) when that module sat at exactly the
500-line cap ``scripts/checks/file_size.py --strict`` enforces and the
ISSUE-0137 guard still had to grow inside it.  The seam is the one
:mod:`.replay_sweep` already states: ``close_path`` owns what closing
ONE record means — the two-phase write, its skips and its
``store_episode`` call — while this module owns the fans that close
MANY of them from a single trigger and hand each to that one-record
path.  Nothing here writes a row; every persist is the caller's
``_persist_closed_interaction`` bound in.

Two fans live here.  :func:`persist_fanned_closes` is the guard every
room-wide close (session end, cost, end-vote, close notification, and
the ingest-time boundary below) persists through, so one record's
failure never discards its siblings.  :func:`close_stale_records` is
the ingest-time boundary fan: the RFC 0030 wire rotation and the
ISSUE-0130 catch-up split, judged per record and persisted behind the
first.

The boot-path fan is NOT here — :mod:`.replay_sweep` keeps
``close_replayed_scopes``, because its cost is startup latency and its
pacing constant belongs with it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

from ..memory.boundary_detectors import REASON_CATCHUP_COMPLETE
from ..memory.interaction_key import record_key, resolve_record_key
from ..memory.interactions import Interaction, InteractionTracker
from .interaction_boundary import stale_close_reason
from .turn_payload import replay_markers

if TYPE_CHECKING:
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "close_stale_records",
    "persist_fanned_closes",
]


async def persist_fanned_closes(
    closed_records: Iterable[Interaction],
    persist: Callable[[Interaction], Awaitable[None]],
) -> None:
    """Persist every record a room-wide close fan just closed — one
    guard per record (v0.3.15 PR 3 review fix).

    The ISSUE-0123 part 3 fans (``close_scope`` at the session-end,
    cost, end-vote and close-notification sites) pop ALL their records
    from the tracker before the first persist runs, so an exception
    escaping one record's persist would silently discard the rest —
    closed, gone from the open map, and with no idle sweep left to find
    them.  Mirror of the per-iteration guard on the idle-flush loops
    (``episode_routing._store_event_episode``, PR-3 review #13): each
    record gets its own ``try`` so a failure is logged with the
    identity of the record that owned it and the fan keeps persisting
    the siblings.
    """
    for interaction in closed_records:
        try:
            await persist(interaction)
        except Exception:
            logger.warning(
                "Failed to persist fanned close (scope=%s, "
                "interaction_id=%s, close_reason=%s)",
                interaction.scope, interaction.interaction_id,
                interaction.close_reason, exc_info=True,
            )


async def close_stale_records(
    tracker: InteractionTracker,
    scope: str,
    event: AgentEvent,
    *,
    wire_id: str,
    persist: Callable[[Interaction], Awaitable[None]],
) -> None:
    """Close every record in ``scope`` this event makes stale, then persist.

    The ingest-time boundary fan, extracted from
    ``_EpisodeRoutingMixin._handle_multi_turn_event`` (PR #846 review —
    that module is at the 500-line cap, and this is a close+guarded-persist
    fan, which is this module's subject).

    :func:`~agents.persona_runtime.interaction_boundary.stale_close_reason`
    owns WHICH boundary fired: the RFC 0030 wire-id rotation (the previous
    channel conversation ended, so the new turn must open fresh under the
    wire-carried cause — producer plan OQ 5) and the ISSUE-0130 catch-up
    boundary (replayed and live turns never share a record, in either
    direction).  ISSUE-0123 part 3: a re-keyed room holds N records, so
    this FANS — per record, because every input is per record (wire id,
    predecessor, replay pair).  Persists behind
    :func:`persist_fanned_closes`, so one failure neither aborts the
    remaining splits nor the caller's own ingest.

    The two boundaries do NOT have the same reach, which is why the fan
    tells the predicate which record is the event's own (PR B2 review).
    Rotation is a ROOM event: the channel's conversation ended, so every
    record in the scope is stale and every one closes.  The catch-up
    boundary is a RECORD event: a turn can only merge into the record
    under its own ``(principal, speaker, scope)`` key, so that is the
    only one it can spoil.  Fanning it room-wide meant one replayed
    event closed every unrelated live conversation in the room — all
    speakers, all tenants — chopping each into a one-turn episode and
    firing an unmetered summarise for it, on the boot path, while
    dispatch was serving.

    The target key is resolved the same way ``add_turn`` resolves the one
    it is about to append to: AMBIENT principal (this event's bound
    scope) plus the event's own ``sender_id``.  Resolving it here rather
    than reading it off a record is deliberate — the record the turn will
    land in may not exist yet, which is exactly the replay→live race.
    """
    target = resolve_record_key(scope, None, event.sender_id)
    stale_splits: list[Interaction] = []
    for record in tracker.records_for_scope(scope):
        stale_reason = stale_close_reason(
            record, event, wire_id=wire_id,
            is_target_record=record_key(record) == target,
        )
        if stale_reason is None:
            continue
        # Replay-INTERNAL segmentation, not truncation (PR B2 review).
        # A wire ROTATION between two REPLAYED rows is a genuine
        # conversation boundary inside the window — RFC 0037's
        # replayed-rotation stamping is exactly this — and the segment it
        # closes holds a WHOLE wire conversation, so its digest is
        # boot-stable: the next boot replays the same window, forms the
        # same segment and computes the same id.  Only the segment still
        # OPEN when a pass is cut short is a prefix, and the pass-end
        # sweep is what refuses that one.
        #
        # THREE conjuncts narrow it to that case, and the first cut had
        # none of them (PR B2 review):
        #
        # * a LIVE event closing a replayed record is the opposite case —
        #   the record is then whatever replay had ingested when the live
        #   turn interrupted it;
        # * so is the ISSUE-0130 ATTRIBUTION split, which
        #   ``stale_close_reason`` also answers ``REASON_CATCHUP_COMPLETE``
        #   for when a seeded ``"local"`` row and an unseeded one resolve
        #   to the same record key.  That closes a prefix, not a
        #   conversation, so only a rotation reason may mark complete; and
        # * a window this pass has ALREADY cut or holed cannot have a
        #   whole conversation left in it.  Without this the door derived
        #   the remainder of an already-cut window, and derived segments
        #   with a hole where a row had raised — both claiming an id no
        #   later boot recomputes, which is the growth curve the gate
        #   exists to bound, reached through the one door that did not
        #   ask.  Same predicate the pass-end sweep applies.
        channel = record.source_channel_id or ""
        segment_complete = (
            record.replayed
            and replay_markers(event)[0]
            and stale_reason != REASON_CATCHUP_COMPLETE
            and bool(channel)
            and not tracker.replay_record_compromised(
                channel, record.speaker_id,
            )
        )
        split = tracker.close_record(
            record, reason=stale_reason,
            # ``None`` leaves the fail-closed default standing AND lets
            # ``close_record`` record the truncation this pass.
            replay_window_complete=True if segment_complete else None,
        )
        if split is not None:
            stale_splits.append(split)
    await persist_fanned_closes(stale_splits, persist)
