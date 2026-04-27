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
from typing import TYPE_CHECKING

from ..observability.metrics import current_agent_id, try_get_instruments
from .boundary_detectors import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    REASON_IDLE_GAP,
    REASON_STRUCTURAL,
    BoundaryDetector,
    default_detectors,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._open: dict[str, Interaction] = {}
        # Detector chain is evaluated in order; first ``(True, reason)``
        # wins.  Default chain: structural → idle-gap → topic-shift no-op.
        self._detectors: tuple[BoundaryDetector, ...] = (
            tuple(detectors)
            if detectors is not None
            else default_detectors(idle_timeout_sec=idle_timeout_sec)
        )

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
        ts = now if now is not None else time.time()
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
        ts = now if now is not None else time.time()
        interaction = self._open.get(scope)
        if interaction is None or not interaction.is_open:
            interaction = self.start(scope, now=ts)
        interaction.turns.append(Turn(at=ts, payload=payload or {}))
        return interaction

    def close(
        self,
        scope: str,
        *,
        reason: str = REASON_STRUCTURAL,
        now: float | None = None,
    ) -> Interaction | None:
        """Close the interaction in ``scope`` (no-op if none open).

        Returns the closed interaction (with ``closed_at`` and
        ``close_reason`` populated) so callers — Phase 1 single-turn
        paths in PR 2, the structural-trigger hooks in PR 5 — can hand
        it to the persistence layer in one step.
        """
        interaction = self._open.pop(scope, None)
        if interaction is None:
            return None
        ts = now if now is not None else time.time()
        interaction.closed_at = ts
        interaction.close_reason = reason
        _emit_closed(reason)
        return interaction

    def idle_check(
        self, *, now: float | None = None,
    ) -> list[Interaction]:
        """Evaluate every open interaction against the detector chain.

        Returns the list of newly-closed interactions in evaluation
        order.  Wired into the periodic janitor by PR 4; PR 1 exercises
        this method only via unit tests.
        """
        ts = now if now is not None else time.time()
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


__all__ = [
    "Interaction",
    "InteractionTracker",
    "SCOPE_TICK",
    "Turn",
    "scope_for_dm",
    "scope_for_group",
    "scope_for_thread",
]
