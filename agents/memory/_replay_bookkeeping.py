"""Per-PASS replay bookkeeping for :class:`InteractionTracker`.

Split out of :mod:`agents.memory.interaction_tracker` (v0.3.15 PR B2
review round 3), which sat at the 500-line cap
``scripts/checks/file_size.py --strict`` enforces.  The seam is a real one:
everything here is scoped to a single on-startup catch-up PASS and is
meaningless outside it, where the rest of the tracker is scoped to a
record's lifetime.  It is also the only tracker state that is cleared
wholesale rather than per record.

Two facts, both about the ISSUE-0130 (b) derivation gate:

* which channels had a replayed record TRUNCATED — closed by a door other
  than the pass-end sweep, so what is still open for them is the remainder
  of an already-cut window; and
* which wire message ids this boot ingested LIVE, so the catch-up replay
  can tell that a row it is about to replay is one dispatch already
  delivered.  Dispatch self-registers before catch-up runs, so that
  overlap is a race rather than an edge case.
"""

from __future__ import annotations

__all__ = ["_ReplayBookkeepingMixin"]


class _ReplayBookkeepingMixin:
    """The catch-up-pass state :class:`InteractionTracker` carries."""

    _replayed_closes: dict[str, int]
    _live_message_ids: set[str]

    def _init_replay_bookkeeping(self) -> None:
        """Called from ``InteractionTracker.__init__``."""
        self._replayed_closes = {}
        self._live_message_ids = set()

    # ─── truncation ────────────────────────────────────────

    def note_replayed_close(self, channel_id: str) -> None:
        """Record that a replayed record for ``channel_id`` was TRUNCATED.

        Replay-internal segmentation sets ``replay_window_complete`` before
        closing and is deliberately not counted here: it leaves a whole
        wire conversation behind, not a prefix, so it must not disqualify
        the rest of the window.
        """
        self._replayed_closes[channel_id] = (
            self._replayed_closes.get(channel_id, 0) + 1
        )

    def replayed_closes_by_channel(self) -> dict[str, int]:
        """Replay-opened records truncated this pass, per source channel.

        A channel counted here had a replayed record cut short, so whatever
        is still open for it is the remainder of an already-cut window.
        ``Interaction.replay_window_complete`` states what that costs;
        ``replay_sweep.close_replayed_scopes`` is the only reader.
        """
        return dict(self._replayed_closes)

    # ─── the same-boot live/replay overlap ─────────────────

    def observe_wire_message(
        self, message_id: str, *, replayed: bool,
    ) -> bool:
        """Note one turn's wire id; report whether it is a live DUPLICATE.

        One call for both halves, because they are the same fact seen from
        the two sides and a caller that did one without the other would be
        silently wrong: a live turn RECORDS its id, a replayed turn ASKS
        about it.

        ``True`` means this replayed message is in a live record too, so
        summarising the replayed copy would derive the same content twice
        in one boot.  Dispatch self-registers before catch-up runs, so a
        message published in that gap reaches both paths — and the
        re-derivation guard cannot see across them, because a live
        record's ``interaction_id`` is a ``uuid4`` and not a content
        digest.

        A caller acting on ``True`` must still APPEND the turn and merely
        mark it (:data:`~agents.memory.interaction_types
        .LIVE_DUPLICATE_TURN_KEY`).  Dropping it would make the replay span
        digest depend on which messages happened to race — boot-unstable,
        so the next boot would derive the window again under a different
        id.  Keeping the turn and excluding it only from the DERIVATION
        INPUT keeps the identity stable and the content unduplicated.

        Ingest dedup was rejected for the CROSS-boot case (lock 4) because
        the tracker is in-memory and a restart starts blind to the previous
        boot.  That is exactly why it works WITHIN one.
        """
        if not message_id:
            return False
        if not replayed:
            self._live_message_ids.add(message_id)
            return False
        return message_id in self._live_message_ids

    # ─── end of pass ───────────────────────────────────────

    def clear_replay_pass_state(self) -> None:
        """Forget both, at the END of a catch-up sweep.

        Scopes them to ONE PASS rather than to the tracker's lifetime: a
        second catch-up in the same process (RFC 0011 OQ #8's reconnect
        re-catch-up) must not inherit pass 1's cuts and refuse pass 2's
        whole windows forever, and once catch-up is over every turn is live
        so the id set is only memory.
        """
        self._replayed_closes.clear()
        self._live_message_ids.clear()
