"""Per-PASS replay bookkeeping for :class:`InteractionTracker`.

Split out of :mod:`agents.memory.interaction_tracker` (v0.3.15 PR B2
review round 3), which sat at the 500-line cap
``scripts/checks/file_size.py --strict`` enforces.  The seam is a real one:
everything here is scoped to a single on-startup catch-up PASS and is
meaningless outside it, where the rest of the tracker is scoped to a
record's lifetime.  It is also the only tracker state that is cleared
wholesale rather than per record.

Two facts, both about the ISSUE-0130 (b) derivation gate:

* which ``(channel, speaker)`` replay windows are COMPROMISED — cut short
  by a door other than the pass-end sweep, or holed by a row that raised
  on the way in — so what is still open for them is a prefix or the
  remainder of an already-cut window rather than a whole one.  Keyed per
  SPEAKER because records are, and because a channel-wide spelling made
  one live turn racing catch-up in a busy room cost every other speaker
  there their derivation, on every boot; and
* which wire message ids this boot ingested LIVE, so the catch-up replay
  can tell that a row it is about to replay is one dispatch already
  delivered.  Dispatch self-registers before catch-up runs, so that
  overlap is a race rather than an edge case.

Both are bounded by the PASS, and the pass is a real interval with two
ends: it opens at process start (before catch-up, because the live turns
that can race it arrive that early) and closes at
:meth:`clear_replay_pass_state`.  Recording past the close is what turned
``_live_message_ids`` into an unbounded per-message leak.
"""

from __future__ import annotations

__all__ = ["_ReplayBookkeepingMixin"]


class _ReplayBookkeepingMixin:
    """The catch-up-pass state :class:`InteractionTracker` carries."""

    _compromised_records: set[tuple[str, str]]
    _compromised_channels: set[str]
    _live_message_ids: set[str]
    _replay_pass_open: bool

    def _init_replay_bookkeeping(self) -> None:
        """Called from ``InteractionTracker.__init__``.

        ``_replay_pass_open`` starts ``True`` because the window this
        bookkeeping covers opens at PROCESS START, not at the first
        replayed turn: dispatch self-registers before catch-up runs
        (``agents.server.AgentServer.start``), so the live turns that can
        race the replay arrive before the pass does.
        """
        self._compromised_records = set()
        self._compromised_channels = set()
        self._live_message_ids = set()
        self._replay_pass_open = True

    # ─── compromised replay windows ────────────────────────

    def note_replayed_close(self, channel_id: str, speaker_id: str) -> None:
        """Record that a replayed record was TRUNCATED mid-window.

        Keyed ``(channel, speaker)`` — the same granularity the catch-up
        pass records its raised-row gaps at, and the granularity records
        themselves have.  Keying this by CHANNEL alone (as the first cut
        did) meant one live turn racing catch-up in a busy room cost
        every OTHER speaker in that room their derivation, on every boot
        (PR B2 review) — the identical defect the review had already
        fixed one field over for ``speaker_gaps``.

        Replay-internal segmentation sets ``replay_window_complete``
        before closing and is deliberately not counted here: it leaves a
        whole wire conversation behind, not a prefix, so it must not
        disqualify the rest of the window.
        """
        self._compromised_records.add((channel_id, speaker_id))

    def note_replay_gap(self, channel_id: str, speaker_id: str) -> None:
        """Record that a replayed ROW never reached the tracker.

        The catch-up loop's ``on_event`` raise path (PR B2 review): the
        record that row belonged to now holds a gap this boot invented, so
        its span digest is not one a later boot recomputes.

        A blank ``speaker_id`` is a row whose sender could not be read —
        the gap could be in ANY record, so it compromises the whole
        channel.  Recorded HERE as well as on ``ReplayPassOutcome`` because
        both derivation doors have to see it: the pass-end sweep reads the
        outcome, but the ingest-time segmentation door in
        ``close_path.close_stale_records`` closes and persists a record
        mid-pass, long before the outcome is read.
        """
        if speaker_id:
            self._compromised_records.add((channel_id, speaker_id))
        else:
            self._compromised_channels.add(channel_id)

    def replay_record_compromised(
        self, channel_id: str, speaker_id: str,
    ) -> bool:
        """May this ``(channel, speaker)``'s replayed record still derive?

        ``True`` means no: something already cut or holed this window, so
        whatever is still open for it is a prefix or a remainder rather
        than a whole window.  ``Interaction.replay_window_complete`` states
        what that costs.  Read LIVE by both doors rather than snapshotted —
        under the ``(channel, speaker)`` key a record can only ever be
        compromised by its OWN close, and the two writers set the flag
        before closing, so there is nothing for a snapshot to protect
        against and a stale one would miss a truncation landing mid-sweep.
        """
        return (
            channel_id in self._compromised_channels
            or (channel_id, speaker_id) in self._compromised_records
        )

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
            # Only while the pass is OPEN (PR B2 review).  The set has no
            # reader once catch-up is over — nothing replayed can arrive
            # again — but the writer used to run for the process lifetime,
            # so a long-lived persona retained one wire id per message it
            # had ever ingested, with nothing to bound or free it.  The
            # ``clear`` below is the only disarm and it fires once.
            if self._replay_pass_open:
                self._live_message_ids.add(message_id)
            return False
        return message_id in self._live_message_ids

    # ─── end of pass ───────────────────────────────────────

    def clear_replay_pass_state(self) -> None:
        """Forget all of it, at the END of a catch-up sweep.

        Scopes it to ONE PASS rather than to the tracker's lifetime: a
        second catch-up in the same process (RFC 0011 OQ #8's reconnect
        re-catch-up) must not inherit pass 1's cuts and refuse pass 2's
        whole windows forever.

        It also CLOSES the pass, which is what bounds
        ``_live_message_ids``.  Recording live wire ids is only useful
        while a replayed turn can still ask about one; afterwards the set
        is write-only, and leaving the writer armed grew it by one entry
        per ingested message for the rest of the process's life.  Must
        therefore run even on a catch-up path that replayed nothing —
        ``channel_catchup.replay_for_persona_agents`` calls the sweep for
        every persona agent it can see, including when it has no
        orchestrator session to fetch from.
        """
        self._compromised_records.clear()
        self._compromised_channels.clear()
        self._live_message_ids.clear()
        self._replay_pass_open = False
