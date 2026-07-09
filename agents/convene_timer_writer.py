"""RFC 0052 §E — the ``agents.yaml`` convene-timer writer (v0.3.11 PR 7c-ii-b).

The consumer half of the config round-trip whose PRODUCER is the Go
``internal/channels/standing_schedule.go`` ``ChannelRouter.StandingConveneTimers``
(PR 7c-i, dark): that method derives, from every armed standing channel, the
:class:`internal/channels.ConveneTimerSpec` a config round-trip must register in
the convener persona's ``agents.yaml`` ``autonomy.timers`` set. This module WRITES
that entry — given a convener's autonomy block (as loaded from ``agents.yaml``) and
the standing channels it convenes, it returns the merged block that arms each
channel's convene timer, so a fired ``ScheduledWake(callback_kind="convene")``
reaches the :mod:`agents.tick` handler (PR 7c-ii-a) and, through the ``/convene``
endpoint, the SAME bounded ``ChannelRouter.ConveneChannel`` path a human operator
hits (PR 7b's ``max_convenings`` / ``standing_budget_tokens`` ceilings included —
the timer must never bypass the §E bounds built for it).

Two correctness contracts the naive "append a ``timers`` entry" gets wrong, both
flagged in ``standing_schedule.go`` (its lines 49-62) as deferred to this writer:

  * **Level bump.** ``server_persona.initialize_persona_agents`` builds the
    ``TickScheduler`` (the ``EventLoop`` a timer arms on) ONLY when
    ``autonomy.level`` is ``semi-autonomous`` / ``autonomous``; a ``reactive`` /
    ``passive`` convener silently ignores a ``timers`` entry, and — per the shipped
    gate — so does ``supervisor``. So the writer raises a below-scheduler level to
    :data:`_MIN_SCHEDULER_LEVEL` (the minimum that runs a scheduler) and leaves an
    already-scheduling level untouched (never downgrades ``autonomous``).

  * **Tick carry-forward.** ``server_persona`` passes
    ``register_legacy_timer=(timers is None)``: a convener that *today* ticks on
    ``tick_interval_seconds`` with **no** ``timers`` block loses that heartbeat the
    instant a ``timers`` block appears. So — and ONLY when — the convener was
    ALREADY scheduling (``level`` in :data:`_SCHEDULER_LEVELS`) with no ``timers``
    block, the writer first materializes that implicit legacy tick as an explicit
    ``{id: legacy_tick, kind: tick}`` entry at its effective interval, then adds
    the convene entry. A convener that was ``reactive`` (bumped just now) had no
    tick to carry — it gets the convene timer ALONE, so gaining a schedule does not
    silently start ordinary autonomy LLM spend. A convener already on the ``timers``
    path (any ``timers`` value, including ``[]``) keeps its explicit set verbatim —
    its ``register_legacy_timer`` is already ``False``, so no heartbeat is at risk.

Pure and idempotent: the input block is never mutated; a convene entry refreshes in
place rather than duplicating (a duplicate id would doubly-arm the same channel);
and the result is timer-id sorted, so a config-round-trip diff is stable across
applications (matching the producer's ``StandingConveneTimers`` ordering). The
carry-forward is gated on "no ``timers`` block", which the first application always
establishes, so re-deriving from the writer's own output is a no-op.

``tick_interval_seconds`` is left in place after carry-forward (``timers`` wins, so
it is dead but harmless — ``server_persona`` logs an INFO breadcrumb and ignores
it); removing it is a larger, comment-losing edit this minimal writer avoids.

Ships DARK — like the Go producer, nothing calls this in-tree yet; the round-trip
is an operator/config-time action (no runtime ``RegisterTimer`` API — RFC 0052
OQ #4), and ``MT-AUTONOMOUS-003`` exercises it end to end.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .convene_timer import STANDING_CONVENE_KIND, standing_convene_timer_id

__all__ = ["ConveneSpec", "merge_convene_timers"]

# The ``autonomy.level`` values at which ``server_persona.initialize_persona_agents``
# builds a ``TickScheduler`` — mirrors its ``if level in (...)`` gate. A convene
# timer only arms on a scheduler, so a convener must resolve to one of these.
_SCHEDULER_LEVELS = ("semi-autonomous", "autonomous")

# The minimum scheduler level a below-scheduler convener is bumped to — the least
# autonomy that runs a scheduler, so the bump grants exactly the capability the
# timer needs and no more.
_MIN_SCHEDULER_LEVEL = "semi-autonomous"

# The legacy tick id/kind the shipped ``TickScheduler`` registers
# (``ScheduledWake(timer_id="legacy_tick", callback_kind="tick")`` — mirrors
# ``agents.tick._LEGACY_TIMER_ID``, pinned by the writer test). Reusing them makes
# a carried-forward entry byte-identical to the wake the persona would otherwise
# have fired, so it shares the same ``scheduled_wakes`` cache row.
_LEGACY_TICK_ID = "legacy_tick"
_LEGACY_TICK_KIND = "tick"

# The interval the legacy tick fires at when ``tick_interval_seconds`` is unset —
# mirrors ``autonomy.get("tick_interval_seconds", 60)`` in server_persona.py, so a
# carried-forward tick keeps the exact cadence the persona ran at.
_DEFAULT_TICK_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class ConveneSpec:
    """One standing channel a convener re-convenes on schedule — the writer input.

    The convener is implicit (it is the persona whose autonomy block is being
    merged; the caller selects it by matching the producer's ``ConvenerID``), so a
    spec carries only the channel and its interval. ``channel_id`` is a canonical
    ``group:<name>`` address; ``interval_seconds`` is the channel's
    ``schedule_interval_seconds`` (a positive integer, satisfying the schema's 1.0s
    floor).
    """

    channel_id: str
    interval_seconds: int


def _upsert_by_id(timers: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    """Replace the entry sharing ``entry``'s id, else append — so a re-applied
    convene entry refreshes its interval in place instead of double-arming."""
    for i, existing in enumerate(timers):
        if existing.get("id") == entry["id"]:
            timers[i] = entry
            return
    timers.append(entry)


def merge_convene_timers(
    autonomy: Mapping[str, Any] | None,
    specs: Iterable[ConveneSpec],
) -> dict[str, Any]:
    """Return ``autonomy`` merged with a convene timer for each spec.

    Applies the level bump and tick carry-forward contracts (see the module
    docstring). Pure: ``autonomy`` and its ``timers`` entries are copied, never
    mutated. Raises :class:`ValueError` for a non-group ``channel_id`` — a standing
    channel is group-only, so a non-group spec is a caller bug the writer refuses
    to encode into a malformed entry.
    """
    src = dict(autonomy) if autonomy else {}
    level = src.get("level", "reactive")
    was_scheduling = level in _SCHEDULER_LEVELS
    # ``"timers" in src`` — present-but-``None`` and present-``[]`` are both the
    # timers path (``register_legacy_timer`` already ``False``); only a wholly
    # absent key leaves the legacy tick live.
    had_timers_block = src.get("timers") is not None

    timers: list[dict[str, Any]] = [dict(t) for t in (src.get("timers") or [])]

    # Tick carry-forward: only a convener that was already scheduling with no
    # timers block has a live legacy heartbeat to preserve. A just-bumped reactive
    # convener had none; an already-timers convener already made its tick explicit.
    if was_scheduling and not had_timers_block:
        interval = src.get("tick_interval_seconds", _DEFAULT_TICK_INTERVAL_SECONDS)
        _upsert_by_id(
            timers,
            {
                "id": _LEGACY_TICK_ID,
                "interval_seconds": interval,
                "kind": _LEGACY_TICK_KIND,
            },
        )

    for spec in specs:
        timer_id = standing_convene_timer_id(spec.channel_id)
        if timer_id is None:
            raise ValueError(
                f"cannot arm a standing convene timer for non-group channel "
                f"{spec.channel_id!r} (standing channels are group-only)"
            )
        _upsert_by_id(
            timers,
            {
                "id": timer_id,
                "interval_seconds": spec.interval_seconds,
                "kind": STANDING_CONVENE_KIND,
            },
        )

    # Deterministic timer-id order — a stable round-trip diff, matching the Go
    # producer's ``StandingConveneTimers`` sort.
    timers.sort(key=lambda t: t["id"])

    merged = dict(src)
    merged["timers"] = timers
    if not was_scheduling:
        merged["level"] = _MIN_SCHEDULER_LEVEL
    return merged
