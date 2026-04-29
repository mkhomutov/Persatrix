"""
InteractionTracker — RFC 0020 §C/D Phase 1 in-memory lifecycle.

Tracks open interactions per scope (RFC 0020 §G), accumulates turns,
and closes them when a :class:`BoundaryDetector` fires.  No LLM calls
in this PR — closing produces a placeholder summary text and leaves
LLM-driven summarisation to PR 4.

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

The ``agent.interactions.summary.failed`` counter is registered in PR 1
but only emitted in PR 4 once the LLM summary path lands.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..observability.metrics import current_agent_id, try_get_instruments
from .boundary_detectors import (
    DEFAULT_CLOSING_GRACE_SEC,
    DEFAULT_IDLE_TIMEOUT_SEC,
    REASON_IDLE_GAP,
    REASON_STRUCTURAL,
    BoundaryDetector,
    default_detectors,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    import aiosqlite

logger = logging.getLogger(__name__)

# ─── Summary text sentinels (RFC 0020 §C) ────────────────────
#
# ``SUMMARY_PENDING_TEXT`` marks a closing-state row whose LLM
# summarisation has not yet completed (e.g. the writer crashed between
# ``INSERT`` and the post-LLM ``UPDATE``).  The PR 4 janitor
# (:func:`cleanup_closing_interactions`) sweeps these rows once they
# exceed ``closing_grace_sec`` and replaces the sentinel with
# ``SUMMARY_UNAVAILABLE_TEXT``.  The sentinel must be unique enough that
# an honest LLM summary cannot collide with it; the bracketed form keeps
# it readable in operator dashboards while making accidental collisions
# implausible.
#
# ``SUMMARY_UNAVAILABLE_TEXT`` is the public fallback rendered to
# downstream consumers (relationship-memory recall, persona prompt
# assembly) when summarisation could not produce a real summary.  It is
# deliberately ASCII-only so legacy log pipelines (CP-1252, GBK) cannot
# re-encode it into ``?``.
SUMMARY_PENDING_TEXT: str = "[summary pending]"
SUMMARY_UNAVAILABLE_TEXT: str = "[interaction summary unavailable]"


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
    """Zero-arg callable returning a wall-clock float (seconds)."""

    def __call__(self) -> float: ...


_DEFAULT_CLOCK: Clock = time.time

# RFC 0020 PR 4 reintroduces the module-level ``logger`` at the point of
# first use — :func:`cleanup_closing_interactions` (the closing-state
# janitor) and any future summary-failure path want a per-module
# logger so operators can filter on ``agents.memory.interactions``.
# Declared at the top of the module above (see ``logger =
# logging.getLogger(__name__)``) so this banner is only a pointer.


# ─── Scope vocabulary (RFC 0020 §G + §D scope-prefix table) ─────

# Scope strings carry the channel-type prefix from RFC 0020 §D so the
# `idx_episodes_scope` index plays well with `LIKE 'thread:%'` style
# scans.  Helper builders keep the format in one place; ad-hoc string
# concatenation at call sites is intentionally avoided so the prefix
# vocabulary cannot drift from the storage-model spec.

SCOPE_TICK: str = "tick"


def scope_for_dm(local_agent_id: str, peer_id: str) -> str:
    """DM scope: deterministic, symmetric in the two participants."""
    a, b = sorted((local_agent_id, peer_id))
    return f"dm:{a}:{b}"


def scope_for_thread(thread_id: str) -> str:
    return f"thread:{thread_id}"


def scope_for_group(channel_name: str) -> str:
    return f"group:{channel_name}"


# ─── Data model ─────────────────────────────────────────────


@dataclass
class Turn:
    """A single turn aggregated into an open interaction.

    PR 1 stores only the timestamp and a small payload dict — the
    payload is opaque to the tracker and is consumed by the PR 4
    summariser.  Storing the message body itself is deliberately out
    of scope per RFC 0020 §D ("Per-turn message text is not stored
    in episodes"); the live buffer for working-memory injection lives
    in working memory, not here.
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
    consumes — channel-side hooks (PR 5) set it before invoking
    :meth:`InteractionTracker.idle_check`.
    """

    interaction_id: str
    scope: str
    started_at: float
    turns: list[Turn] = field(default_factory=list)
    closed_at: float | None = None
    close_reason: str = ""
    structural_close_reason: str = ""

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

    # ── Read-only accessors (used by tests + janitor wiring in PR 4) ──

    def open_scopes(self) -> list[str]:
        return list(self._open)

    def get(self, scope: str) -> Interaction | None:
        return self._open.get(scope)

    # ── Lifecycle ──

    def start(self, scope: str, *, now: float | None = None) -> Interaction:
        """Open a new interaction in ``scope``.

        If an interaction is already open in this scope, returns it
        unchanged — callers should prefer :meth:`add_turn` which handles
        both the open-and-append and start-then-append cases.
        """
        existing = self._open.get(scope)
        if existing is not None and existing.is_open:
            return existing
        ts = now if now is not None else self._clock()
        interaction = Interaction(
            interaction_id=str(uuid.uuid4()),
            scope=scope,
            started_at=ts,
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
    ) -> Interaction:
        """Append a turn, opening an interaction in ``scope`` if needed.

        Returns the open interaction so callers can read its
        ``interaction_id`` for downstream tagging (e.g. trace spans).
        """
        ts = now if now is not None else self._clock()
        interaction = self._open.get(scope)
        if interaction is None or not interaction.is_open:
            interaction = self.start(scope, now=ts)
        interaction.turns.append(Turn(at=ts, payload=payload or {}))
        return interaction

    def close(
        self,
        scope: str,
        *,
        reason: str,
        now: float | None = None,
    ) -> Interaction | None:
        """Close the interaction in ``scope`` (no-op if none open).

        Returns the closed interaction (with ``closed_at`` and
        ``close_reason`` populated) so callers — Phase 1 single-turn
        paths in PR 2, the structural-trigger hooks in PR 5 — can hand
        it to the persistence layer in one step.

        ``reason`` is keyword-required by design (PR-214 review fix,
        Should-Fix #1).  An earlier draft defaulted to
        :data:`REASON_STRUCTURAL`, which would silently mislabel the
        ``agent.interactions.closed.by_structural`` counter if a future
        caller (PR 4 janitor / PR 5 channel hook) forgot to pass the
        kwarg.  Telemetry correctness outranks one-character ergonomics;
        callers must spell the reason explicitly using one of the
        ``REASON_*`` constants in :mod:`.boundary_detectors`.
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
                should_close, reason = detector.evaluate(interaction, now=ts)
                if should_close:
                    finished = self.close(scope, reason=reason, now=ts)
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


def _emit_closed(reason: str) -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    attrs = {"agent_id": current_agent_id(), "reason": reason}
    inst.interactions_closed.add(1, attrs)
    if reason == REASON_IDLE_GAP:
        inst.interactions_closed_by_idle_gap.add(1, attrs)
    elif reason == REASON_STRUCTURAL:
        inst.interactions_closed_by_structural.add(1, attrs)


# ─── Closing-state janitor (RFC 0020 PR 4) ─────────────────


async def cleanup_closing_interactions(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    grace_sec: float = DEFAULT_CLOSING_GRACE_SEC,
    now: float | None = None,
) -> int:
    """Backfill the fallback summary on stuck ``closing``-state rows.

    A ``closing``-state row is one whose ``closed_at`` column is set
    (the tracker successfully closed the interaction and the persistence
    layer wrote the row) but whose ``summary`` column still carries
    :data:`SUMMARY_PENDING_TEXT` — the marker the summariser writes
    before its LLM call.  Such a row indicates the post-LLM ``UPDATE``
    never landed (process crash, network partition, summariser timeout
    that exceeded the synchronous wait window).

    The janitor is best-effort and idempotent: it scans rows whose
    ``closed_at`` predates ``now − grace_sec`` and rewrites their
    summary to :data:`SUMMARY_UNAVAILABLE_TEXT` while incrementing the
    ``agent.interactions.summary.failed`` counter once per row.

    Returns the number of rows updated.  Callers may invoke this from
    a periodic tick or operator-driven recovery script; the function
    does not own its own scheduling.
    """
    ts = now if now is not None else time.time()
    cutoff = ts - grace_sec
    cursor = await db.execute(
        "UPDATE episodes SET summary = ? "
        "WHERE agent_id = ? AND summary = ? AND closed_at IS NOT NULL "
        "AND closed_at < ?",
        (SUMMARY_UNAVAILABLE_TEXT, agent_id, SUMMARY_PENDING_TEXT, cutoff),
    )
    updated = cursor.rowcount or 0
    if updated > 0:
        await db.commit()
        inst = try_get_instruments()
        if inst is not None:
            attrs = {"agent_id": current_agent_id(), "reason": "janitor"}
            for _ in range(updated):
                inst.interactions_summary_failed.add(1, attrs)
        logger.info(
            "Janitor backfilled %d closing-state interaction(s) for agent %s",
            updated, agent_id,
        )
    return updated


__all__ = [
    "Clock",
    "Interaction",
    "InteractionTracker",
    "SCOPE_TICK",
    "SUMMARY_PENDING_TEXT",
    "SUMMARY_UNAVAILABLE_TEXT",
    "Turn",
    "cleanup_closing_interactions",
    "scope_for_dm",
    "scope_for_group",
    "scope_for_thread",
]
