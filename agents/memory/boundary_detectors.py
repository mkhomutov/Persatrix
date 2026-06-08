"""
Interaction boundary detectors (RFC 0020 §B).

This module defines the :class:`BoundaryDetector` Protocol — the seam
through which :class:`agents.memory.interactions.InteractionTracker`
decides when to close an open interaction — and ships the three default
implementations the RFC's hybrid policy calls for:

* :class:`StructuralCloseDetector` — deterministic, immediate triggers
  (thread archive, channel leave, explicit ``END_INTERACTION`` action,
  process shutdown).  Highest priority.
* :class:`IdleGapDetector` — closes an interaction whose most recent
  turn is older than ``idle_timeout_sec`` (default 600s).  Workhorse.
* :class:`TopicShiftDetector` — Phase 4 (post-v0.3.0) hook.  The default
  implementation always returns ``False`` so v0.3.0 ships behavioural
  parity with structural + idle-gap only.

PR 1 (this PR) ships the interfaces and registries; PR 3 wires
:class:`IdleGapDetector` into the runtime, and PR 5 wires structural
triggers into the channel pipeline.  The detectors are deliberately
state-free — all interaction state lives on the :class:`Interaction`
object passed in — so Phase 4's topic-shift implementation can be
swapped in behind a config flag without touching the tracker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from .interactions import Interaction


# ─── Boundary reasons (RFC 0020 §B) ─────────────────────────

# Symbolic strings mirror the metric label space so the metric counter
# name (`interactions.closed.by_<reason>`) and the close-call reason
# stay in lockstep.  Tracker close paths emit one of these; new reasons
# require both a counter and an entry here.
#
# Each constant carries its own ``Literal[...]`` annotation so mypy
# narrows the value through to call sites — the union of all five is
# the :data:`CloseReason` alias defined just below, which is the
# argument type of :meth:`InteractionTracker.close`.  A typo at the
# call site is therefore an arg-type error rather than a silent
# subtotal-counter miss (RFC 0020 PR 6 slice 2 #2).
REASON_STRUCTURAL: Literal["structural"] = "structural"
REASON_IDLE_GAP: Literal["idle_gap"] = "idle_gap"
REASON_TOPIC_SHIFT: Literal["topic_shift"] = "topic_shift"
REASON_MAX_TURNS: Literal["max_turns"] = "max_turns"
REASON_SHUTDOWN: Literal["shutdown"] = "shutdown"
# RFC 0030 Layer 1 (v0.3.8): the per-interaction cost ceiling
# (``interaction_budget_tokens``) exhausting mid-conversation is an
# explicit close trigger — the brainstorm is bounded by spend, not by
# idle/structural signals.  The persona runtime routes the wallet's
# ``interaction_budget_exhausted`` denial through
# :meth:`InteractionTracker.close` with this reason so the interaction
# is summarised (RFC 0020 close path) rather than merely stopping fanout.
REASON_COST: Literal["cost"] = "cost"

# Closed value set for :meth:`InteractionTracker.close`'s ``reason``
# kwarg.  Lives next to the constants so the Protocol below can
# reference it without importing :mod:`.interactions` (would be
# circular — interactions imports the constants from this module).
CloseReason = Literal[
    "structural",
    "idle_gap",
    "max_turns",
    "topic_shift",
    "shutdown",
    "cost",
]


# ─── Default thresholds (RFC 0020 §B and Security Considerations) ────

# Idle timeout: 10 minutes.  Configurable per channel via
# `channel.idle_timeout_sec` and globally via
# `optimization.yaml` → `interaction.idle_timeout_sec`.  PR 1 ships
# the constant only; runtime config wiring lands in PR 3.
DEFAULT_IDLE_TIMEOUT_SEC: float = 600.0

# Closing-state janitor grace window (RFC 0020 §C "Closing rows").  A
# `closing` row stuck longer than this gets the fallback summary text.
# Used by the PR 4 janitor; declared here so the constant lives next to
# the related boundary thresholds.
DEFAULT_CLOSING_GRACE_SEC: float = 300.0

# Hard cap on accumulated turns per interaction (RFC 0020 §Security
# Considerations — "Resource amplification via long interactions").
DEFAULT_MAX_INTERACTION_TURNS: int = 200


# ─── Protocol ───────────────────────────────────────────────


@runtime_checkable
class BoundaryDetector(Protocol):
    """Decide whether an open interaction should be closed.

    Implementations are pure and must not mutate the supplied
    :class:`Interaction`.  ``now`` is passed in (rather than read from
    ``time.time()``) so tests can drive deterministic clock advances and
    so the future ``Clock`` seam from RFC 0021 P1 can be threaded
    through without re-plumbing the detectors.

    Returns ``(should_close, reason)``.  ``reason`` is a short symbolic
    string (one of the ``REASON_*`` constants — i.e. a
    :data:`CloseReason`) used as a metric label and surfaced on the
    close call.  The return shape is a *tagged* union — the empty
    reason is paired exclusively with ``False`` — so ``if should_close``
    narrows ``reason`` to :data:`CloseReason` at the call site without
    a cast (used by :meth:`InteractionTracker.idle_check`).
    """

    def evaluate(
        self, interaction: Interaction, *, now: float,
    ) -> tuple[Literal[True], CloseReason] | tuple[Literal[False], Literal[""]]: ...


# ─── Concrete detectors ─────────────────────────────────────


@dataclass(frozen=True)
class StructuralCloseDetector:
    """Highest-priority detector — deterministic, free.

    PR 1 ships only the marker-evaluation logic: a tracker call site
    that observes a structural event (thread archive, channel leave,
    explicit ``END_INTERACTION`` action, process shutdown) sets
    ``Interaction.structural_close_reason`` to the appropriate
    ``REASON_*`` constant before invoking ``idle_check``.  The
    channel-side hooks that *write* the marker land in PR 5, jointly
    with RFC 0011 PR plan PR 5.
    """

    def evaluate(
        self, interaction: Interaction, *, now: float,
    ) -> tuple[Literal[True], CloseReason] | tuple[Literal[False], Literal[""]]:
        reason: str = getattr(interaction, "structural_close_reason", "")
        # The marker is written by channel-side hooks (PR 5) using a
        # ``REASON_*`` constant; ``cast`` here narrows the read-back
        # without runtime cost.  An invalid marker would surface as a
        # subtotal-counter miss in ``_emit_closed`` — same blast radius
        # as the prior ``str`` annotation but with the contract pinned
        # at the Protocol seam.
        if reason:
            return True, cast("CloseReason", reason)
        return False, ""


@dataclass(frozen=True)
class IdleGapDetector:
    """Close an interaction whose most-recent turn is older than the gap.

    Default ``idle_timeout_sec`` is :data:`DEFAULT_IDLE_TIMEOUT_SEC`
    (600s / 10 min) per RFC 0020 §B.  The constructor takes the timeout
    so per-channel overrides (RFC 0020 §G) can instantiate one detector
    per scope without monkey-patching.

    Returns ``(False, "")`` for an interaction that has no turns yet
    (newly-opened scope before the first ``add_turn``); the caller
    treats that as "nothing to close".
    """

    idle_timeout_sec: float = DEFAULT_IDLE_TIMEOUT_SEC

    def evaluate(
        self, interaction: Interaction, *, now: float,
    ) -> tuple[Literal[True], CloseReason] | tuple[Literal[False], Literal[""]]:
        last_turn_at = interaction.last_turn_at
        if last_turn_at is None:
            return False, ""
        if now - last_turn_at >= self.idle_timeout_sec:
            return True, REASON_IDLE_GAP
        return False, ""


@dataclass(frozen=True)
class TopicShiftDetector:
    """Phase 4 (post-v0.3.0) topic-shift hook — default no-op.

    The Protocol is honoured for compatibility; the default
    implementation always returns ``(False, "")``.  Phase 4 will swap
    in an embedding-similarity or LLM-judge implementation behind a
    config flag (RFC 0020 §B item 3).
    """

    def evaluate(
        self, interaction: Interaction, *, now: float,
    ) -> tuple[Literal[True], CloseReason] | tuple[Literal[False], Literal[""]]:
        return False, ""


@dataclass(frozen=True)
class MaxTurnsDetector:
    """Hard cap on turns per interaction (RFC 0020 §Security).

    PR 3 wires multi-turn aggregation: turns accumulate in
    :class:`~agents.memory.interactions.Interaction.turns` until the
    next structural close or idle gap.  Without a cap, a peer that
    streams messages within the idle window can grow the per-scope
    turn list (and the eventual ``context_json`` blob) without bound
    — the exact "Resource amplification via long interactions" surface
    that RFC 0020 §Security Considerations names.

    Detector closes with :data:`REASON_MAX_TURNS` when an interaction
    has reached or exceeded :data:`DEFAULT_MAX_INTERACTION_TURNS` (or a
    caller-supplied override).  Wired into :func:`default_detectors`
    after structural / idle so explicit closes still take precedence
    and the cap acts as the safety net.

    Added per PR-216 review (Should-Fix #1).  Per-reason subtotal
    counter ``agent.interactions.closed.by_max_turns`` is registered
    alongside the structural / idle-gap subtotals and dispatched from
    the same table in :mod:`agents.memory.interactions._emit_closed`
    (RFC 0020 PR 6 slice 2 #3 — was previously listed here as a "PR 4
    metrics pass" gap that did not land).
    """

    max_turns: int = DEFAULT_MAX_INTERACTION_TURNS

    def evaluate(
        self, interaction: Interaction, *, now: float,
    ) -> tuple[Literal[True], CloseReason] | tuple[Literal[False], Literal[""]]:
        if interaction.turn_count >= self.max_turns:
            return True, REASON_MAX_TURNS
        return False, ""


# ─── Detector registry ──────────────────────────────────────


def default_detectors(
    *,
    idle_timeout_sec: float = DEFAULT_IDLE_TIMEOUT_SEC,
    max_turns: int = DEFAULT_MAX_INTERACTION_TURNS,
) -> tuple[BoundaryDetector, ...]:
    """Return the v0.3.0 default detector chain in priority order.

    Tracker code evaluates the chain left-to-right and closes on the
    first ``(True, reason)`` result.  Structural triggers therefore
    pre-empt idle-gap; the max-turns safety net runs after idle so an
    idle-then-overflow case is still labelled by the proximate cause
    (idle); topic-shift is last (and a no-op in v0.3.0) so Phase 4's
    swap-in is a one-line registry change.

    PR-216 review (Should-Fix #1) added :class:`MaxTurnsDetector` to
    the default chain so :data:`DEFAULT_MAX_INTERACTION_TURNS` is
    actually enforced — previously the constant was exported but never
    consulted, leaving the RFC 0020 §Security amplification surface
    open.
    """
    return (
        StructuralCloseDetector(),
        IdleGapDetector(idle_timeout_sec=idle_timeout_sec),
        MaxTurnsDetector(max_turns=max_turns),
        TopicShiftDetector(),
    )


__all__ = [
    "BoundaryDetector",
    "CloseReason",
    "DEFAULT_CLOSING_GRACE_SEC",
    "DEFAULT_IDLE_TIMEOUT_SEC",
    "DEFAULT_MAX_INTERACTION_TURNS",
    "IdleGapDetector",
    "MaxTurnsDetector",
    "REASON_COST",
    "REASON_IDLE_GAP",
    "REASON_MAX_TURNS",
    "REASON_SHUTDOWN",
    "REASON_STRUCTURAL",
    "REASON_TOPIC_SHIFT",
    "StructuralCloseDetector",
    "TopicShiftDetector",
    "default_detectors",
]
