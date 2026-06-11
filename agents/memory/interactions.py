"""
InteractionTracker — RFC 0020 §C/D Phase 1 in-memory lifecycle.

Tracks open interactions per scope (RFC 0020 §G), accumulates turns,
and closes them when a :class:`BoundaryDetector` fires.

The tracker is process-local: each agent instance owns its own
:class:`InteractionTracker`.  RFC 0020 §C "Restart behavior" pins the
in-memory-only contract — open interactions are deliberately not
persisted; pre-restart turns are recoverable from message storage but
are not auto-summarised.

Counters emitted via :func:`agents.observability.metrics.try_get_instruments`
(non-raising — tests and import-time callers may run before
``init_metrics()``):

* ``agent.interactions.opened`` — every new interaction
* ``agent.interactions.closed`` — every close (any reason)
* ``agent.interactions.closed.by_idle_gap`` — idle-gap closures
* ``agent.interactions.closed.by_structural`` — structural closures

Module split (PR-262 follow-up — file-size cap):

* :mod:`agents.memory.scopes` — scope vocabulary helpers
  (``scope_for_dm`` / ``scope_for_thread`` / ``scope_for_group`` /
  ``scope_for_channel_event``).
* :mod:`agents.memory.interaction_janitor` — closing-state janitor
  (``cleanup_closing_interactions``) and the summary-text sentinels
  (``SUMMARY_PENDING_TEXT`` / ``SUMMARY_UNAVAILABLE_TEXT``).

This module re-exports those public symbols so existing imports
(``from agents.memory.interactions import scope_for_dm`` etc.) keep
working without churn at the call sites.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..observability.metrics import current_agent_id, try_get_instruments
from ..session_id import LEGACY_SESSION_ID
from .boundary_detectors import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    REASON_COST,
    REASON_IDLE_GAP,
    REASON_MAX_TURNS,
    REASON_SHUTDOWN,
    REASON_STRUCTURAL,
    REASON_TOPIC_SHIFT,
    BoundaryDetector,
    CloseReason,
    MaxTurnsDetector,
    default_detectors,
)
from .interaction_janitor import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
    cleanup_closing_interactions,
)
from .scopes import (
    SCOPE_TICK,
    is_thread_scope,
    scope_for_channel_event,
    scope_for_dm,
    scope_for_group,
    scope_for_thread,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# ─── Clock seam (RFC 0020 PR 3) ────────────────────────────

# PR 3 wires :class:`IdleGapDetector` into the runtime path.  The
# tracker has always taken ``now`` as a keyword on every lifecycle
# method, but the *default* came from a bare ``time.time()`` call at
# the call site.  PR 3 introduces a ``Clock`` Protocol so a single
# ``InteractionTracker(clock=...)`` injection point covers every
# default-now codepath.  This is the seam RFC 0021 P1 swaps when its
# ``Clock`` lands — at that point this module's ``Clock`` Protocol gets
# replaced by an alias to the canonical one and the runtime keeps
# working unchanged.  Until then the default clock is ``time.time``.
#
# TODO(rfc-0021-p1): replace ``Clock`` with the RFC 0021 P1 canonical
# clock once it lands; one-line import + alias change.


# Not ``@runtime_checkable``: the Protocol is a single-method callable
# shape, ``time.time`` (and any zero-arg callable) would satisfy a
# bare ``isinstance`` check trivially, and no call site uses one.
# Dropped per PR-216 review (Nice-to-have #1) to keep the dependency
# surface honest; reintroduce only if a future caller actually needs
# a structural check.
class Clock(Protocol):
    """Zero-arg callable returning a wall-clock float (seconds).

    Naming-collision note: :class:`agents.clock.Clock` (added in
    RFC 0021 P1 PR 1) is a *different* Protocol on the same name —
    that one exposes ``now()`` and ``now_iso()`` methods, not a bare
    ``__call__``. The two are scheduled to be aliased in a follow-up
    so ``time.time`` keeps satisfying this Protocol while the canonical
    surface lives in :mod:`agents.clock`. Until that alias lands, do
    not import ``Clock`` from the wrong module — a misimport is
    mypy-clean (the two Protocols are structurally distinct) and only
    fails at the call site. See :mod:`agents.clock` module docstring
    for the full plan.
    """

    def __call__(self) -> float: ...


_DEFAULT_CLOCK: Clock = time.time


# :data:`CloseReason` is imported from :mod:`.boundary_detectors`
# (lives next to the ``REASON_*`` constants — the type and the values
# travel together) and is the argument type of
# :meth:`InteractionTracker.close` below.  Adding a new reason is a
# coordinated edit:
#
#     1. Add ``REASON_<NEW>`` and extend the ``CloseReason`` Literal in
#        :mod:`.boundary_detectors`.
#     2. Register the matching ``agent.interactions.closed.by_<new>``
#        counter in :class:`agents.observability.metrics._Instruments`.
#     3. Map it in :data:`_REASON_COUNTER_ATTR`.
#
# The dispatch table below makes step 3 the only edit to the emit
# path (RFC 0020 PR 6 slice 2 #3 — replaces the prior hand-coded
# ``if/elif`` chain that silently skipped subtotal counters for
# ``max_turns`` / ``topic_shift`` / ``shutdown``).


# ─── Data model ─────────────────────────────────────────────


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
    episode routing.  In-memory only — never persisted.  The agent's
    own ``interaction_id`` stays the memory key (producer plan OQ 1
    defers unification); this field exists solely so the routing path
    can detect the channel's rotation boundary and close the local
    scope structurally in step with the wire.  Its sibling
    ``predecessor_wire_id`` (PR 607 second pass; also in-memory only)
    is the retired wire id the opening turn attributed
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


# ─── Tracker ────────────────────────────────────────────────


class InteractionTracker:
    """Per-agent, in-memory tracker keyed by scope (RFC 0020 §G).

    One open interaction per scope at a time.  Calls to
    :meth:`add_turn` for an unknown scope start a new interaction; for
    a scope with an open interaction they append a turn and reset the
    idle timer (the timer state lives on the turn timestamp, not on a
    separate field).

    Closing is invoked either explicitly via :meth:`close` (Phase 1
    single-turn paths and PR 5 structural triggers) or by the periodic
    janitor calling :meth:`idle_check` (wired in PR 4).  The reopen
    rule from RFC 0020 §C — "do not reopen" — is enforced by removing
    the closed interaction from the scope map immediately, so a
    subsequent ``add_turn`` for the same scope opens a fresh one.
    """

    def __init__(
        self,
        *,
        detectors: Iterable[BoundaryDetector] | None = None,
        idle_timeout_sec: float = DEFAULT_IDLE_TIMEOUT_SEC,
        clock: Clock | None = None,
    ) -> None:
        self._open: dict[str, Interaction] = {}
        # Detector chain is evaluated in order; first ``(True, reason)``
        # wins.  Default chain: structural → idle-gap → topic-shift no-op.
        self._detectors: tuple[BoundaryDetector, ...] = (
            tuple(detectors)
            if detectors is not None
            else default_detectors(idle_timeout_sec=idle_timeout_sec)
        )
        # Clock seam (PR 3).  Replaces the prior ``time.time()`` defaults
        # at every lifecycle method's ``now`` argument so tests inject a
        # deterministic clock once at construction time instead of
        # threading ``now=`` through every call.
        self._clock: Clock = clock if clock is not None else _DEFAULT_CLOCK
        # PR-3 review #12: cache the cap from whichever
        # :class:`MaxTurnsDetector` is in the chain so :meth:`add_turn`
        # can enforce it inline (rather than waiting for the next
        # ``idle_check`` sweep, which let a structural close in between
        # mislabel the closure as ``REASON_STRUCTURAL`` and surface the
        # RFC 0020 §Security amplification window).  Sourcing the cap
        # from the chain — rather than a separate constructor kwarg —
        # keeps the cap-config knob in one place; a test that swaps in
        # a custom chain without :class:`MaxTurnsDetector` correctly
        # sees no inline cap.
        #
        # Cache invariant (PR-3 review #300/N2): this lookup runs once
        # at construction.  Safe today because ``_detectors`` is a tuple
        # (no reassignment below) and :class:`MaxTurnsDetector` is
        # ``frozen=True``; a future refactor that mutates the chain
        # (e.g. ``replace_detectors``) must refresh this cache to stay
        # aligned with :meth:`idle_check`'s detector-walking view.
        self._max_turns: int | None = next(
            (d.max_turns for d in self._detectors if isinstance(d, MaxTurnsDetector)),
            None,
        )

    # ── Read-only accessors (used by tests + janitor wiring in PR 4) ──

    def open_scopes(self) -> list[str]:
        return list(self._open)

    def get(self, scope: str) -> Interaction | None:
        return self._open.get(scope)

    # ── Lifecycle ──

    def start(
        self,
        scope: str,
        *,
        now: float | None = None,
        session_id: str | None = None,
    ) -> Interaction:
        """Open a new interaction in ``scope``.

        If an interaction is already open in this scope, returns it
        unchanged — callers should prefer :meth:`add_turn` which handles
        both the open-and-append and start-then-append cases.

        ``session_id`` (ISSUE-0081 PR 2) is the RFC 0031 session frozen
        onto the new interaction; ``None`` / blank collapses to the
        ``legacy`` carve-out.  It is *only* honoured when a fresh
        interaction is opened — an already-open scope keeps the session it
        was born under, so a later turn arriving on a different scope
        cannot relabel it.
        """
        existing = self._open.get(scope)
        if existing is not None and existing.is_open:
            return existing
        ts = now if now is not None else self._clock()
        interaction = Interaction(
            interaction_id=str(uuid.uuid4()),
            scope=scope,
            started_at=ts,
            session_id=session_id or LEGACY_SESSION_ID,
        )
        self._open[scope] = interaction
        _emit_opened()
        return interaction

    def add_turn(
        self,
        scope: str,
        payload: dict[str, object] | None = None,
        *,
        now: float | None = None,
        session_id: str | None = None,
    ) -> Interaction:
        """Append a turn, opening an interaction in ``scope`` if needed.

        Returns the interaction the turn landed in.  PR-3 review #12:
        when the just-appended turn pushes ``turn_count`` to the
        :class:`MaxTurnsDetector` cap, the interaction is closed inline
        with :data:`REASON_MAX_TURNS` and popped from the open map —
        the returned object's ``is_open`` is then ``False`` and the
        caller is expected to hand it to the persistence layer in the
        same step.  Closing inline (rather than waiting for the next
        ``idle_check`` sweep) ensures the cap-fired closure is
        attributed to ``max_turns`` even when a structural event
        arrives between the cap-th turn and the next event.

        ``session_id`` (ISSUE-0081 PR 2) is forwarded to :meth:`start`
        and so is captured only when this call *opens* the interaction;
        a turn appended to an already-open scope ignores it (the session
        is frozen at open).
        """
        ts = now if now is not None else self._clock()
        interaction = self._open.get(scope)
        if interaction is None or not interaction.is_open:
            interaction = self.start(scope, now=ts, session_id=session_id)
        interaction.turns.append(Turn(at=ts, payload=payload or {}))
        if (
            self._max_turns is not None
            and interaction.turn_count >= self._max_turns
        ):
            # ``close`` pops ``scope`` from ``self._open`` and returns
            # the same interaction (with ``closed_at`` / ``close_reason``
            # set) that we just appended to.  Returning the closed object
            # lets callers observe ``is_open is False`` and route it
            # straight to ``_persist_closed_interaction`` without an
            # extra tracker lookup.
            closed = self.close(scope, reason=REASON_MAX_TURNS, now=ts)
            if closed is not None:
                return closed
        return interaction

    def close(
        self,
        scope: str,
        *,
        reason: CloseReason,
        now: float | None = None,
    ) -> Interaction | None:
        """Close the interaction in ``scope`` (no-op if none open).

        Returns the closed interaction (with ``closed_at`` and
        ``close_reason`` populated) so callers — Phase 1 single-turn
        paths in PR 2, the structural-trigger hooks in PR 5 — can hand
        it to the persistence layer in one step.

        ``reason`` is keyword-required by design (PR-214 review fix,
        Should-Fix #1) and typed as :data:`CloseReason` (RFC 0020 PR 6
        slice 2 #2) so a typo at the call site is caught by mypy
        instead of silently mislabelling the
        ``agent.interactions.closed.by_<reason>`` counter.  An earlier
        draft also defaulted to :data:`REASON_STRUCTURAL`, which would
        have hidden a future caller forgetting the kwarg; telemetry
        correctness outranks one-character ergonomics, so callers must
        spell the reason explicitly using one of the ``REASON_*``
        constants in :mod:`.boundary_detectors`.
        """
        interaction = self._open.pop(scope, None)
        if interaction is None:
            return None
        ts = now if now is not None else self._clock()
        interaction.closed_at = ts
        interaction.close_reason = reason
        _emit_closed(reason)
        return interaction

    def idle_check(
        self, *, now: float | None = None,
    ) -> list[Interaction]:
        """Evaluate every open interaction against the detector chain.

        Returns the list of newly-closed interactions in evaluation
        order.  PR 3 wires this into the per-event hot path of
        :class:`~agents.persona_runtime.state_persistence._StatePersistenceMixin`
        so an idle multi-turn interaction is closed the moment the next
        event arrives in *any* scope; PR 4 will additionally drive it
        from a periodic janitor independent of event traffic.
        """
        ts = now if now is not None else self._clock()
        closed: list[Interaction] = []
        # Copy the scope list because :meth:`close` mutates ``self._open``.
        for scope in list(self._open):
            interaction = self._open[scope]
            for detector in self._detectors:
                # Access the tagged-union tuple via indexing so mypy
                # narrows ``result[1]`` to :data:`CloseReason` on the
                # ``result[0] is True`` branch (unpacking would erase
                # the correlation between the two slots).
                result = detector.evaluate(interaction, now=ts)
                if result[0]:
                    finished = self.close(scope, reason=result[1], now=ts)
                    if finished is not None:
                        closed.append(finished)
                    break
        return closed


# ─── Metrics emission helpers ───────────────────────────────


def _emit_opened() -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    inst.interactions_opened.add(1, {"agent_id": current_agent_id()})


# Reason → ``_Instruments`` attribute holding the per-reason subtotal
# counter.  Keep this table in lockstep with :data:`CloseReason` and
# the counter registrations in
# :class:`agents.observability.metrics._Instruments` — adding a new
# reason without an entry here drops its subtotal silently (the prior
# ``if/elif`` chain had the same property; the dict shape just makes
# the breakage table-shaped instead of statement-shaped, and a future
# enum migration is a one-line swap).
_REASON_COUNTER_ATTR: dict[CloseReason, str] = {
    REASON_STRUCTURAL: "interactions_closed_by_structural",
    REASON_IDLE_GAP: "interactions_closed_by_idle_gap",
    REASON_MAX_TURNS: "interactions_closed_by_max_turns",
    REASON_TOPIC_SHIFT: "interactions_closed_by_topic_shift",
    REASON_SHUTDOWN: "interactions_closed_by_shutdown",
    REASON_COST: "interactions_closed_by_cost",
}


def _emit_closed(reason: CloseReason) -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    attrs = {"agent_id": current_agent_id(), "reason": reason}
    inst.interactions_closed.add(1, attrs)
    counter_attr = _REASON_COUNTER_ATTR.get(reason)
    if counter_attr is not None:
        getattr(inst, counter_attr).add(1, attrs)


__all__ = [
    "Clock",
    "Interaction",
    "InteractionTracker",
    "SCOPE_TICK",
    "SUMMARY_PENDING_TEXT",
    "SUMMARY_UNAVAILABLE_TEXT",
    "Turn",
    "cleanup_closing_interactions",
    "is_thread_scope",
    "scope_for_channel_event",
    "scope_for_dm",
    "scope_for_group",
    "scope_for_thread",
]
