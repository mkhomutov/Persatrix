"""``InteractionTracker`` — RFC 0020 §C/D lifecycle, keyed per §G.

Extracted from :mod:`agents.memory.interactions` when the v0.3.15
residuals PR 3 re-key pushed that module past the 500-line cap
(``scripts/checks/file_size.py --strict``); that module stays the
public façade and re-exports every name here, so existing imports
(``from agents.memory.interactions import InteractionTracker``) keep
working without call-site churn.

The tracker key (ISSUE-0123 R-1 + ISSUE-0131, Phase 0/0b — decided on
live evidence, see ``docs/issues/ISSUE-0082-residuals-phase0-gate.md``)
is the tuple ``(principal_id, speaker_id, scope)``:

* ``scope`` — the RFC 0020 §G room/thread/DM unit, unchanged.  It stays
  the persisted ``episodes.scope`` string and the prefix-predicate /
  ``idx_episodes_scope`` surface; the two new axes are NOT encoded into
  it (each has its own column since migrations v11 and 18).
* ``principal_id`` — the tenant the turn ran under, resolved from the
  ambient :func:`~agents.principal_id.principal_scope` (else the
  env/default) at each call.  A group room no longer accumulates every
  authenticated person's turns into one record that closes under
  whichever principal happened to trigger the close.
* ``speaker_id`` — the event's ``sender_id``.  The principal is a
  *tenant* axis and only authenticated humans have one, so a room full
  of personas shares the ``local`` principal; the speaker half is what
  keeps agent A's turns out of the record B's restatement is derived
  from (the Phase 0b misattribution).  ``""`` = no speaker (tick /
  single-turn scopes whose event carries none).

Both halves are frozen onto the :class:`Interaction` at open (the
``session_id`` footing) — they ARE the key, so a later turn under a
different principal or speaker lands in a different record.  The DM
topology already answered this design: ``scope_for_dm`` keys on the
sender, so a DM record has always been per-speaker; the tuple key
makes group and thread scopes consistent with it.

Room-wide closes (ISSUE-0123 part 3): a structural close, an end-vote
quorum, the bounded/cost close and the close-notification turn are
ROOM events, and a room now holds N records — :meth:`close_scope` is
the fan (see its docstring for the admission rules), and
:meth:`append_turn` lands a room event's closing turn on each record
before it.  Consequence, stated not discovered:
the ``agent.interactions.closed[.by_<reason>]`` counters fire once per
RECORD, so a room close increments them by N (see
:mod:`.interaction_metrics`).
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Protocol

from ..principal_id import normalize_principal_id, resolve_principal_id_silent
from ..session_id import LEGACY_SESSION_ID
from .boundary_detectors import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    REASON_MAX_TURNS,
    BoundaryDetector,
    CloseReason,
    MaxTurnsDetector,
    default_detectors,
)
from .interaction_metrics import _emit_closed, _emit_opened
from .interaction_types import Interaction, Turn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

__all__ = ["Clock", "InteractionTracker"]


# ─── Clock seam (RFC 0020 PR 3) ────────────────────────────

# A single ``InteractionTracker(clock=...)`` injection point covers
# every default-now codepath, and is the seam RFC 0021 P1 swaps when its
# ``Clock`` lands.  TODO(rfc-0021-p1): one-line import + alias change.


# Not ``@runtime_checkable``: any zero-arg callable would satisfy a bare
# ``isinstance`` trivially, and no call site uses one (PR-216 review).
class Clock(Protocol):
    """Zero-arg callable returning a wall-clock float (seconds).

    Naming collision: :class:`agents.clock.Clock` (RFC 0021 P1) is a
    DIFFERENT Protocol on the same name; a misimport is mypy-clean and
    fails only at the call site — see :mod:`agents.clock`.
    """

    def __call__(self) -> float: ...


_DEFAULT_CLOCK: Clock = time.time


# The tracker key: ``(principal_id, speaker_id, scope)`` — module doc.
_RecordKey = tuple[str, str, str]


def _record_key(interaction: Interaction) -> _RecordKey:
    """The key an open ``interaction`` is registered under — stable for
    its lifetime, the three components being frozen at open."""
    return (interaction.principal_id, interaction.speaker_id, interaction.scope)


class InteractionTracker:
    """Per-agent, in-memory tracker keyed ``(principal, speaker, scope)``.

    One open interaction per key at a time.  Calls to :meth:`add_turn`
    for an unknown key start a new interaction; for a key with an open
    interaction they append a turn and reset the idle timer (the timer
    state lives on the turn timestamp, not on a separate field).

    Key resolution is uniform across :meth:`start` / :meth:`add_turn` /
    :meth:`get` / :meth:`close` and spelled out on :meth:`_key`:
    ``principal_id=None`` is AMBIENT, ``speaker_id=None`` / blank is
    ``""``.  Single-tenant deployments with senderless scopes collapse
    to one key per scope — exactly the pre-v0.3.15 shape.

    Closing is invoked explicitly per record (:meth:`close` /
    :meth:`close_record`), room-wide (:meth:`close_scope` — the
    ISSUE-0123 part 3 fan), or by the janitor via :meth:`idle_check`
    (per record: a speaker who went quiet idles out on their own
    timer).  The reopen rule from RFC 0020 §C — "do not reopen" — is
    enforced by removing the closed interaction from the key map
    immediately, so a subsequent ``add_turn`` for the same key opens a
    fresh one.
    """

    def __init__(
        self,
        *,
        detectors: Iterable[BoundaryDetector] | None = None,
        idle_timeout_sec: float = DEFAULT_IDLE_TIMEOUT_SEC,
        clock: Clock | None = None,
    ) -> None:
        self._open: dict[_RecordKey, Interaction] = {}
        # Detector chain is evaluated in order; first ``(True, reason)``
        # wins.  Default chain: structural → idle-gap → topic-shift no-op.
        self._detectors: tuple[BoundaryDetector, ...] = (
            tuple(detectors)
            if detectors is not None
            else default_detectors(idle_timeout_sec=idle_timeout_sec)
        )
        # Clock seam (PR 3): tests inject a deterministic clock once here
        # instead of threading ``now=`` through every call.
        self._clock: Clock = clock if clock is not None else _DEFAULT_CLOCK
        # PR-3 review #12: cache the cap so :meth:`add_turn` enforces it
        # inline, not on the next ``idle_check`` sweep (which let a
        # structural close in between mislabel the closure and surface
        # the RFC 0020 §Security amplification window).  Cache invariant
        # (review #300/N2): this runs ONCE at construction — safe while
        # ``_detectors`` is a tuple and ``MaxTurnsDetector`` is frozen; a
        # refactor that mutates the chain must refresh it.
        self._max_turns: int | None = next(
            (d.max_turns for d in self._detectors if isinstance(d, MaxTurnsDetector)),
            None,
        )

    # ── Key resolution ──

    @staticmethod
    def _key(
        scope: str, principal_id: str | None, speaker_id: str | None,
    ) -> _RecordKey:
        """Resolve the ``(principal, speaker, scope)`` key for a call.

        ``principal_id=None`` means AMBIENT (never "default"): the
        task-local scope wins, then the env var, then ``local`` — the
        same precedence the storage tiers resolve writes under, so the
        record a turn lands in and the tenant its close-derived rows
        bind (residuals PR 4) cannot disagree.  An explicit value goes
        through :func:`~agents.principal_id.normalize_principal_id` so
        ``""`` cannot mint a key no recall predicate matches.
        """
        principal = (
            normalize_principal_id(principal_id)
            if principal_id is not None
            else resolve_principal_id_silent()
        )
        return (principal, (speaker_id or "").strip(), scope)

    # ── Read-only accessors (tests + janitor wiring) ──

    def now(self) -> float:
        """One clock-seam read — a room fan stamps its N appends and
        closes with a SINGLE instant (one room event, one timestamp)."""
        return self._clock()

    def open_scopes(self) -> list[str]:
        """Distinct scopes with at least one open record, insertion order.

        Since the re-key this is a PROJECTION — one scope may hold several
        records; callers needing them use :meth:`records_for_scope`.
        """
        return list(dict.fromkeys(i.scope for i in self._open.values()))

    def open_records(self) -> list[Interaction]:
        """Every open record, insertion order (all scopes, all keys)."""
        return list(self._open.values())

    def records_for_scope(self, scope: str) -> list[Interaction]:
        """The open records in ``scope``, insertion order — one per
        ``(principal, speaker)`` pair; the room-wide fan's working set."""
        return [i for i in self._open.values() if i.scope == scope]

    def get(
        self,
        scope: str,
        *,
        principal_id: str | None = None,
        speaker_id: str | None = None,
    ) -> Interaction | None:
        """The open record under the resolved key, or ``None``.

        Key resolution as the class docstring: omitted axes resolve
        ambient/no-speaker, so a legacy ``get(scope)`` still finds the
        record a legacy ``add_turn(scope)`` opened.
        """
        return self._open.get(self._key(scope, principal_id, speaker_id))

    # ── Lifecycle ──

    def start(
        self,
        scope: str,
        *,
        now: float | None = None,
        session_id: str | None = None,
        classification: str | None = None,
        source_channel_id: str | None = None,
        replayed: bool = False,
        principal_id: str | None = None,
        speaker_id: str | None = None,
    ) -> Interaction:
        """Open a new interaction under the resolved key.

        If an interaction is already open under this key, returns it
        unchanged — callers should prefer :meth:`add_turn` which handles
        both the open-and-append and start-then-append cases.

        ``session_id`` (ISSUE-0081 PR 2) is the RFC 0031 session frozen
        onto the new interaction; ``None`` / blank collapses to the
        ``legacy`` carve-out.  It is *only* honoured when a fresh
        interaction is opened — an already-open key keeps the session it
        was born under, so a later turn arriving on a different scope
        cannot relabel it.

        ``classification`` / ``source_channel_id`` (RFC 0037 §C, v0.3.12
        PR 3) are the acting channel's wire classification and channel id,
        frozen at open under exactly the same only-on-open rule — see
        :class:`~agents.memory.interaction_types.Interaction` for the
        verbatim-capture contract.

        ``principal_id`` / ``speaker_id`` (ISSUE-0123 / ISSUE-0131) are
        the other two key axes, resolved per the class docstring and
        frozen onto the record — trivially only-on-open, since a
        different pair IS a different key.
        """
        key = self._key(scope, principal_id, speaker_id)
        existing = self._open.get(key)
        if existing is not None and existing.is_open:
            return existing
        ts = now if now is not None else self._clock()
        interaction = Interaction(
            interaction_id=str(uuid.uuid4()),
            scope=scope,
            started_at=ts,
            session_id=session_id or LEGACY_SESSION_ID,
            replayed=replayed,
            classification=classification,
            source_channel_id=source_channel_id,
            principal_id=key[0],
            speaker_id=key[1],
        )
        self._open[key] = interaction
        _emit_opened()
        return interaction

    def add_turn(
        self,
        scope: str,
        payload: dict[str, object] | None = None,
        *,
        now: float | None = None,
        session_id: str | None = None,
        classification: str | None = None,
        source_channel_id: str | None = None,
        replayed: bool = False,
        principal_id: str | None = None,
        speaker_id: str | None = None,
    ) -> Interaction:
        """Append a turn, opening an interaction under the key if needed.

        Returns the interaction the turn landed in.  PR-3 review #12:
        when the just-appended turn pushes ``turn_count`` to the
        :class:`MaxTurnsDetector` cap, the interaction is closed inline
        with :data:`REASON_MAX_TURNS` and popped from the open map —
        the returned object's ``is_open`` is then ``False`` and the
        caller is expected to hand it to the persistence layer in the
        same step.

        ``session_id`` / ``classification`` / ``source_channel_id`` are
        forwarded to :meth:`start` and so are captured only when this
        call *opens* the interaction (frozen at open).  ``principal_id``
        / ``speaker_id`` resolve the KEY, so unlike those they also
        select which open record the turn appends to — a turn from a
        different speaker or tenant lands in its own record rather than
        being absorbed by a sibling's (the whole point of the re-key).
        """
        ts = now if now is not None else self._clock()
        key = self._key(scope, principal_id, speaker_id)
        interaction = self._open.get(key)
        if interaction is None or not interaction.is_open:
            interaction = self.start(
                scope, now=ts, session_id=session_id,
                classification=classification,
                source_channel_id=source_channel_id,
                replayed=replayed,
                principal_id=key[0], speaker_id=key[1],
            )
        interaction.turns.append(Turn(at=ts, payload=payload or {}))
        if (
            self._max_turns is not None
            and interaction.turn_count >= self._max_turns
        ):
            # ``close_record`` pops the key and returns the same
            # interaction (with ``closed_at`` / ``close_reason`` set)
            # that we just appended to, so callers can observe
            # ``is_open is False`` and route it straight to
            # ``_persist_closed_interaction`` without a tracker lookup.
            closed = self.close_record(
                interaction, reason=REASON_MAX_TURNS, now=ts,
            )
            if closed is not None:
                return closed
        return interaction

    def append_turn(
        self,
        interaction: Interaction,
        payload: dict[str, object] | None = None,
        *,
        now: float | None = None,
    ) -> None:
        """Append a ROOM event's turn to one already-open record.

        The room-wide close fan's ingest half (ISSUE-0123 part 3): a
        close-notification turn must land as the final turn of EVERY
        record open in the scope, and routing it through the ordinary
        per-event path would deliver it to the sender's key alone — or
        fabricate a fresh record where the sender has none.  No-op for
        a record that is no longer open (a concurrent close won).

        Deliberately does NOT enforce the max-turns cap: the only
        caller closes the record in the same step, so a cap-crossing
        final turn would merely relabel an imminent room close as
        ``max_turns`` — the notification's truthful trigger outranks
        the cap label.  Every other ingest goes through
        :meth:`add_turn`, where the cap stands.
        """
        if not interaction.is_open:
            return
        ts = now if now is not None else self._clock()
        interaction.turns.append(Turn(at=ts, payload=payload or {}))

    def close(
        self,
        scope: str,
        *,
        reason: CloseReason,
        now: float | None = None,
        principal_id: str | None = None,
        speaker_id: str | None = None,
    ) -> Interaction | None:
        """Close the interaction under the resolved key (no-op if none).

        ONE record — the key resolution matches :meth:`get`.  A room
        event (structural close, end-vote quorum, bounded/cost close,
        close notification) must use :meth:`close_scope` instead: since
        the re-key a scope holds one record per ``(principal, speaker)``
        pair, and closing one of them leaks the rest open until idle
        (ISSUE-0123 part 3).

        ``reason`` is keyword-required and typed :data:`CloseReason`
        so a typo is caught by mypy rather than mislabelling the
        per-reason counter (PR-214 review fix).
        """
        interaction = self._open.get(self._key(scope, principal_id, speaker_id))
        if interaction is None:
            return None
        return self.close_record(interaction, reason=reason, now=now)

    def close_record(
        self,
        interaction: Interaction,
        *,
        reason: CloseReason,
        now: float | None = None,
    ) -> Interaction | None:
        """Close one specific open record (identity-guarded).

        The record addresses itself: its frozen ``(principal, speaker,
        scope)`` IS its key, so callers that already hold the record —
        the per-record fans, the replay sweep, the just-opened
        single-turn pair — close it directly instead of re-deriving the
        key.  No-op (``None``) when the map no longer holds THIS object
        under that key: a concurrent close already told the truth, and
        popping a same-key successor would close a different
        interaction than the one the caller reasoned about.
        """
        key = _record_key(interaction)
        if self._open.get(key) is not interaction:
            return None
        del self._open[key]
        ts = now if now is not None else self._clock()
        interaction.closed_at = ts
        interaction.close_reason = reason
        _emit_closed(reason)
        return interaction

    def close_scope(
        self,
        scope: str,
        *,
        reason: CloseReason,
        now: float | None = None,
        admit: Callable[[Interaction], bool] | None = None,
    ) -> list[Interaction]:
        """Close EVERY open record in ``scope`` — the room-wide fan.

        ISSUE-0123 part 3: a structural close, an end-vote quorum, the
        bounded/cost close and the close-notification turn are room
        events, and a room holds one record per ``(principal, speaker)``
        pair.  Returns the closed records in insertion order (empty if
        none were open).  Each close emits its own
        ``agent.interactions.closed`` increment — the stated
        metric-shape change (see :mod:`.interaction_metrics`).  One room
        event, one instant: the clock is read ONCE and every sibling's
        ``closed_at`` carries the same value.

        REPLAY-opened records are SKIPPED: a replayed span belongs to
        the catch-up pass and its ``REASON_CATCHUP_COMPLETE`` sweep, so
        a live room cause would mislabel the counter and steal it from
        :func:`close_replayed_scopes`.  The exclusion lives here, not in
        :meth:`close_record` — that is what the sweep closes them with.

        ``admit`` is the caller's extra per-record rule (PR #846
        review): the fans that close only PART of a scope — the
        end-vote quorum and the bounded/cost close, which must not bury
        a successor conversation — pass the shared
        ``interaction_boundary.wire_admits_record`` conjunct here
        rather than hand-rolling the read/filter/close loop, so the
        ordering obligations have ONE owner.  ``None`` admits every
        live record: a session-end close takes the whole room.
        """
        ts = now if now is not None else self._clock()
        closed: list[Interaction] = []
        for interaction in self.records_for_scope(scope):
            if interaction.replayed:
                continue
            if admit is not None and not admit(interaction):
                continue
            finished = self.close_record(interaction, reason=reason, now=ts)
            if finished is not None:
                closed.append(finished)
        return closed

    def idle_check(
        self, *, now: float | None = None,
    ) -> list[Interaction]:
        """Evaluate every open record against the detector chain.

        Returns the list of newly-closed interactions in evaluation
        order.  Wired into the per-event hot path of
        :class:`~agents.persona_runtime.state_persistence._StatePersistenceMixin`
        and the periodic janitor.  Per RECORD deliberately, not per
        scope: the idle window is a property of a record's own last
        turn, so in a room a speaker who went quiet idles out while an
        active sibling record stays open — idle is the one close that
        is not a room event.
        """
        ts = now if now is not None else self._clock()
        closed: list[Interaction] = []
        # Copy because :meth:`close_record` mutates ``self._open``.
        for interaction in list(self._open.values()):
            for detector in self._detectors:
                # Access the tagged-union tuple via indexing so mypy
                # narrows ``result[1]`` to :data:`CloseReason` on the
                # ``result[0] is True`` branch (unpacking would erase
                # the correlation between the two slots).
                result = detector.evaluate(interaction, now=ts)
                if result[0]:
                    finished = self.close_record(
                        interaction, reason=result[1], now=ts,
                    )
                    if finished is not None:
                        closed.append(finished)
                    break
        return closed
