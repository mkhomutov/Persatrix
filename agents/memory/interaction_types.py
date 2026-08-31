"""``Turn`` / ``Interaction`` — the RFC 0020 §C/D lifecycle records.

Split out of :mod:`agents.memory.interactions` when the RFC 0052 PR 4b-ii
``meter_close_summary`` field pushed that module past the 500-line cap
(``scripts/checks/file_size.py --strict``) — the same module-split
precedent as :mod:`agents.memory.scopes` and
:mod:`agents.memory.interaction_janitor`; the tracker module re-exports
both names so existing imports keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..principal_id import DEFAULT_PRINCIPAL_ID
from ..session_id import LEGACY_SESSION_ID

# The one key RFC 0020 §G exempts from single-speaker construction: the
# room-close fan lands the closing message as the final turn of EVERY
# sibling record, so on all but one of them this turn's ``sender`` is not
# the record's ``speaker_id`` (PR #846 review).  Stamped by the producer
# (:func:`~agents.persona_runtime.turn_payload.build_turn_payload`) rather
# than reconstructed per consumer from ``sender`` ≠ ``speaker_id``: that
# reconstruction is a guess — the tracker key strips ``speaker_id`` while
# the payload ``sender`` is verbatim, so whitespace alone defeats it — and
# every consumer would have to make it independently.  It lives here, not
# beside the builder, because it is part of the ``Turn.payload`` contract
# and the read surface (``closed_interactions_read``) must not grow an
# import into the persona subpackage to honour it.  Survives persistence:
# ``persist_closed_interaction`` strips only the keys in its
# ``_TRANSIENT_TURN_KEYS`` set (``text``, and ``message_id`` since
# v0.3.15 PR B2) — this one is not among them.
ROOM_CLOSE_TURN_KEY = "room_close"

# ISSUE-0130 (b), PR B2 review round 3: this REPLAYED turn carries a wire
# message the agent had ALREADY ingested live during this same boot, so its
# content is in a live record too.  Dispatch self-registers before catch-up
# runs, so a message published in that gap reaches both paths — and the
# re-derivation guard cannot see across them, because the live record's
# ``interaction_id`` is a ``uuid4`` and not a content digest.
#
# The turn is kept ON the record and dropped from the DERIVATION INPUT, and
# that asymmetry is the whole point: the span identity is a digest over the
# turns the record HOLDS, so removing the turn would make the digest depend
# on which messages happened to race — boot-unstable, and the next boot
# would derive the window again under a different id.  Excluding it only
# from what gets summarised keeps the identity boot-stable AND the content
# unduplicated.  Stripped before ``context_json`` like the other transient
# keys.
LIVE_DUPLICATE_TURN_KEY = "live_duplicate"


@dataclass
class Turn:
    """A single turn aggregated into an open interaction.

    The payload is opaque to the tracker and is consumed by the
    close-path summariser.  It carries the structural envelope plus —
    since ISSUE-0054 — the inbound message ``text`` the RFC 0026 facts
    extractor needs.  RFC 0020 §D still holds for the *persisted*
    episode: ``_persist_closed_interaction`` strips ``text`` before the
    turn lands in ``context_json``, so the body is carried only
    transiently on the in-memory turn and the episodic store never
    doubles as a message log.  The live buffer for working-memory
    injection lives in working memory, not here.
    """

    at: float
    payload: dict[str, object] = field(default_factory=dict)


@dataclass
class Interaction:
    """An open interaction in a single scope.

    Lifecycle states (RFC 0020 §C) are encoded by ``closed_at``:

    * ``closed_at is None`` → ``open``
    * ``closed_at is not None`` → ``closing`` (the tracker hands the
      interaction off to the persistence layer; the row's lifecycle
      after that is encoded by ``(closed_at, summary)`` per §D).

    The ``structural_close_reason`` field is the marker that
    :class:`~agents.memory.boundary_detectors.StructuralCloseDetector`
    consumes — an out-of-band call site that observes a structural
    event sets it before the next :meth:`InteractionTracker.idle_check`
    sweep.  The *channel-side* structural closes that landed with the
    RFC 0030 interaction-id producer (end-of-interaction vote, wire
    interaction-id rotation — ``persona_runtime/episode_routing.py``)
    run inline on the event path and call
    :meth:`InteractionTracker.close` directly instead, so the marker
    remains the seam for detectors that cannot close synchronously.

    ``wire_interaction_id`` (RFC 0030 interaction-id producer) is the
    orchestrator-minted channel interaction id this local interaction
    was opened under, seeded from the first turn's event metadata by
    episode routing.  In-memory on the live interaction; at close it is
    copied into the persisted episode context as
    ``governance_interaction_id`` (ISSUE-0102 — ``close_path.py``).  The
    agent's own ``interaction_id`` stays the memory key (OQ 1 defers
    unification); the live field also drives rotation-boundary detection.
    Its sibling ``predecessor_wire_id`` (PR 607 second pass; in-memory
    only, not persisted) is the retired wire id the opening turn attributed
    (``previous_interaction_id``) — the rotation seam's late-delivery
    defence, see :func:`~agents.persona_runtime.interaction_boundary
    .wire_rotation_closes`.
    """

    interaction_id: str
    scope: str
    started_at: float
    turns: list[Turn] = field(default_factory=list)
    closed_at: float | None = None
    close_reason: str = ""
    structural_close_reason: str = ""
    wire_interaction_id: str = ""
    predecessor_wire_id: str = ""
    # ISSUE-0081 PR 2: the RFC 0031 session captured when the interaction
    # *opened*, frozen for its lifetime.  Load-bearing for the
    # sibling-mislabel guard: ``idle_check`` can flush conversation B's
    # stale interaction while conversation A's event holds the active
    # scope, so the close-path persistence must tag the row with the
    # session the interaction was born under — not whatever scope is bound
    # at flush time.  Defaults to the ``legacy`` carve-out so a pre-PR-2
    # construction site (or a turn opened with no scope) stays visible.
    session_id: str = LEGACY_SESSION_ID
    # ISSUE-0123 (R-1) / ISSUE-0131 — the two halves of the tracker key
    # beside ``scope`` (v0.3.15 residuals PR 3).  Both sit on the
    # ``session_id`` footing above: resolved when the interaction OPENS,
    # frozen for its lifetime, and — being key components — never
    # re-read from a later turn.  ``principal_id`` is the tenant the
    # opening turn ran under (the ambient ``principal_scope``, else the
    # single-tenant default); ``persist_closed_interaction`` binds THIS
    # value around the derivation pipeline — both phases — so an idle
    # flush or a room-close fan cannot write the record under whichever
    # principal happened to trigger the close (PR #846 review).  ``speaker_id`` is the
    # opening event's ``sender_id`` (``""`` = no speaker: tick /
    # single-turn scopes whose event carries none) — the ISSUE-0131 axis
    # that keeps a room of personas, who all share the ``local``
    # principal, from collapsing into one record.  The persisted
    # ``episodes.speaker_id`` / ``facts.speaker_id`` columns (migration
    # 17 → 18) are a PROJECTION of this key half — attribution is sound
    # only because the record is single-speaker by construction, never
    # model-elected (Phase 0b scope lock) — with ONE stated exception
    # (RFC 0020 §G amendment, PR #846): the room-close fan lands the
    # closing message as the final turn of every sibling record — a
    # foreign-speaker turn, carrying :data:`ROOM_CLOSE_TURN_KEY` so the
    # close path excludes it on a RECORDED fact rather than re-deriving
    # ``sender`` ≠ this ``speaker_id``.  It is dropped at the §G
    # chokepoint every close-pipeline consumer reads through
    # (``close_entries.own_turn_items``), so neither the projected
    # column, an extracted fact, nor the persisted turn context can
    # come from another speaker's words.
    principal_id: str = DEFAULT_PRINCIPAL_ID
    speaker_id: str = ""
    # ISSUE-0085 / ISSUE-0130 (b) review — the EPOCH half of the same
    # frozen-at-open rule, and the last write axis that was still read
    # ambient at close time.  ``principal_id`` above is bound around the
    # derivation so an idle flush or a room-close fan cannot stamp the
    # closer's tenant on this record; the epoch had no such twin, so a
    # record closed inside ANOTHER request's scope was stamped with that
    # request's epoch instead of its own.  ``store_episode`` writes the
    # ambient epoch and every episodic recall filters it with strict
    # equality and no carve-out (:mod:`agents.memory._epoch_filter`), so
    # a mis-stamped row is permanently unreadable by the epoch that
    # produced it.  Since v0.3.15 PR B2 that also decides an IDENTITY:
    # the replay span digest carries the epoch, so an ambient read made
    # the digest depend on WHICH close path fired rather than on the
    # span.
    #
    # ``""`` means the record was opened by a construction site that
    # captures nothing (a direct ``Interaction(...)`` in a test), and the
    # close path then leaves epoch resolution exactly where it was —
    # ambient — so no existing path changes shape.  The tracker fills it
    # for every record it opens.
    epoch_id: str = ""
    # ISSUE-0130: True when this interaction was OPENED by an on-startup
    # catch-up replay turn, captured under the same only-on-open rule as
    # ``session_id`` above.
    #
    # On its own this no longer decides derivation.  Since shape (b)
    # (v0.3.15 PR B2) ``messages.principal_id`` persists the tenant at
    # publish and ``build_replay_event`` seeds it, so a replayed span that
    # knows its tenant DOES derive, under that tenant — see
    # ``replay_attributed`` below, which is the half that decides, and
    # :func:`~agents.persona_runtime.close_path.persist_closed_interaction`
    # for both.  What this flag still governs by itself: the catch-up
    # boundary (a replayed span never shares a record with live turns, in
    # either direction), the room-close fan's eligibility rule
    # (``admitted_records`` excludes replayed records unconditionally),
    # and the re-derivation guard, which only a replayed span consults.
    replayed: bool = False
    # ISSUE-0130 shape (b) — whether the replayed turn that OPENED this
    # record carried a persisted principal (channel-store v12's
    # ``messages.principal_id``, seeded by
    # :func:`~agents.principal_id.seed_principal_metadata`).  Meaningful
    # only while ``replayed`` is set, and frozen at open beside it.
    #
    # It exists because the record key CANNOT answer the question.  A
    # seeded ``local`` and an unseeded default are the same
    # ``principal_id`` on the record, and they mean opposite things: the
    # first is a real answer ("this publish had no verified tenant" —
    # an agent publish, or the whole deployment under
    # ``auth.mode: disabled``), the second is the absence of an answer
    # ("this orchestrator predates the column"), which is precisely the
    # ambiguity the v0.3.14 leak-stopper could not resolve and so
    # resolved conservatively for every span.  Only the field's PRESENCE
    # on the wire separates them, so the presence is what is recorded
    # here — the value is already on ``principal_id`` above.
    replay_attributed: bool = False
    # ISSUE-0130 shape (b) — may this replayed record's span be DERIVED?
    #
    # ``False`` by default, and that polarity is the point (PR B2 review).
    # The span identity is a digest over the turns the record HOLDS, so a
    # record holding a PREFIX of its channel's window claims an id no later
    # boot can recompute — and the next complete boot then derives the whole
    # window on top of it, which is the unbounded growth shape (b) exists to
    # bound.  Only :func:`~agents.persona_runtime.replay_sweep
    # .close_replayed_scopes` can know a record holds a whole window (it runs
    # after the pass, and is told which channels finished), so only it sets
    # this.  Every OTHER close door — the ingest-time replay/live split, a
    # wire rotation reaching a non-target record, ``idle_check``, the
    # ``max_turns`` cap — closes a record mid-pass, which by construction
    # means a partial window, and leaves the default standing.
    #
    # The first cut kept this decision in the sweep's own loop body, so those
    # other doors derived prefixes unguarded.  Moving it onto the record puts
    # it where the ONE chokepoint every door funnels through can read it
    # (``persist_closed_interaction``), and makes the unguarded case the safe
    # one.  Refusing costs a boot's derivation, which catch-up re-reads on the
    # next boot anyway (no watermark, RFC 0011 OQ #8); deriving a prefix costs
    # a duplicate episode that nothing can ever match again.
    #
    # Meaningful only while ``replayed`` is set.
    replay_window_complete: bool = False
    # RFC 0037 §C (v0.3.12 PR 3): the acting channel's wire classification
    # captured when the interaction *opened*, frozen for its lifetime — the
    # single point of truth the episodic and facts tiers inherit their
    # ``protection_level`` from at close (classification is read once per
    # interaction, never re-derived per episode or per fact).  Held VERBATIM
    # (``None`` = the opening event carried no classification — a tick /
    # pre-v0.3.12 producer / non-channel scope): the §A rule-(a) stamping
    # default is applied by the close-path stamp sites through
    # ``persona_runtime/classification.py``'s ``normalize_for_stamp``, the
    # one resolver owning that rule — this memory-side record deliberately
    # imports no lattice (the import direction is persona_runtime → memory).
    # Frozen-at-open like ``session_id`` above: a later turn arriving after
    # an operator reclassifies the channel cannot relabel the open record.
    classification: str | None = None
    # RFC 0037 §C: the channel the interaction's content came from, frozen
    # at open beside ``classification`` — becomes the episode/fact rows'
    # nullable ``source_channel_id`` (NULL for DM-less/tick scopes whose
    # opening event carried no channel id).
    source_channel_id: str | None = None
    # RFC 0052 PR 4b-ii (OQ #6): true iff this interaction was closed by the
    # AUTONOMOUS bounded close (the close notification carried the truthful
    # ``structural``/``cost`` trigger — ``close_notification.py`` sets it
    # between the tracker close and persistence), so the RFC 0020 close
    # summary must draw a wallet lease billed to ``wire_interaction_id``
    # (``summarize_close.py``) and count toward the mandatory cap the PR 4a
    # ``1 + N`` reserve was carved from. In-memory only, like
    # ``predecessor_wire_id`` — never persisted. The default keeps every
    # other close path (human channels, end-vote, idle, cost ceiling)
    # byte-for-byte on the unleased pre-4b-ii summariser call.
    meter_close_summary: bool = False

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def last_turn_at(self) -> float | None:
        if not self.turns:
            return None
        return self.turns[-1].at

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


__all__ = ["ROOM_CLOSE_TURN_KEY", "Interaction", "Turn"]
