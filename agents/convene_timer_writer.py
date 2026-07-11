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
    already-scheduling level untouched (never downgrades ``autonomous``). The bump
    is applied ONLY when a convene timer is actually armed — see the no-op rule
    below.

  * **Tick carry-forward.** ``server_persona`` passes
    ``register_legacy_timer=(timers is None)``: a convener that *today* ticks on
    ``tick_interval_seconds`` with **no** ``timers`` block loses that heartbeat the
    instant a ``timers`` block appears. So — and ONLY when — the convener was
    ALREADY scheduling (``level`` in :data:`_SCHEDULER_LEVELS`) with no ``timers``
    block *and the writer is introducing one*, the writer first materializes that
    implicit legacy tick as an explicit ``{id: legacy_tick, kind: tick}`` entry at
    its effective interval, then adds the convene entry. A convener that was
    ``reactive`` (bumped just now) had no tick to carry — it gets the convene timer
    ALONE, so gaining a schedule does not silently start ordinary autonomy LLM
    spend. A convener already on the ``timers`` path (any ``timers`` value,
    including ``[]``) keeps its explicit set verbatim — its
    ``register_legacy_timer`` is already ``False``, so no heartbeat is at risk.

Reconciling, not appending: the convene-kind timers in the output match ``specs``
exactly — a channel absent from ``specs`` (its standing config disarmed/deleted) has
its convene timer DROPPED, so a stale timer never keeps firing a wake into a channel
that can only decline it. This makes the seam a true round-trip: the writer's output
tracks the producer's current ``StandingConveneTimers`` set, additions AND removals.
Non-convene timers are left untouched — the writer owns the convene kind alone.

**Nothing to arm, nothing to drop → the block is returned unchanged.** The natural
driver for this writer walks every persona in ``agents.yaml`` and passes
``specs_by_convener.get(persona_id, [])``, so the overwhelmingly common call is on a
persona that convenes NOTHING. Neither side effect may fire on that call: an
unconditional level bump would make the whole fleet ``semi-autonomous`` (every
persona then builds an ``EventLoop`` and emits the ``COST: … will consume LLM
tokens continuously`` warnings for a schedule it does not have), and an
unconditional ``timers: []`` would flip ``register_legacy_timer`` off fleet-wide —
latent today, but an operator who later deletes that pointless empty block hands the
bumped persona a ``tick_interval_seconds`` LLM heartbeat it never had. So the level
bump is gated on ``specs`` being non-empty, and a ``timers`` key is written only
when the writer arms something or one already existed (the disarm/reconcile case,
where a stale convene entry must still be dropped). The bump is deliberately
one-way — reconciling a convener back to zero standing channels does NOT lower its
level again, because the writer cannot know whether an operator raised it for an
unrelated reason.

Loud on caller/config bugs rather than silently emitting an entry the convener
cannot arm: a non-group ``channel_id``, a sub-floor ``interval_seconds`` (the
schema's ``minimum: 1.0`` / ``EventLoop._MIN_INTERVAL`` busy-loop guard), and an id
collision with a surviving non-convene timer each raise :class:`ValueError`. Every
one of them would otherwise surface at the convener's NEXT BOOT — as a schema
rejection, an ``EventLoop.register_timer`` raise that aborts persona init, or a
silently clobbered operator timer — far from the config-time action that caused it.

Pure and idempotent: the input block is never mutated; a surviving channel's convene
entry refreshes in place rather than duplicating (a duplicate id would doubly-arm the
same channel); and the result is timer-id sorted, so a config-round-trip diff is
stable across applications. The carry-forward is gated on "no ``timers`` block", which
the first application always establishes, so re-deriving from the writer's own output
is a no-op.

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
# Pinned against the server_persona.py source by the writer test's drift guard.
_SCHEDULER_LEVELS = ("semi-autonomous", "autonomous")

# The level a convener config with no explicit ``level`` resolves to — mirrors
# ``autonomy.get("level", "reactive")`` in server_persona.py. Below-scheduler, so an
# unlevelled convener is bumped rather than silently swallowing its timer.
_DEFAULT_LEVEL = "reactive"

# The minimum scheduler level a below-scheduler convener is bumped to — the least
# autonomy that runs a scheduler, so the bump grants the capability the timer needs
# and no more autonomy than that. It is still a real capability grant (an EventLoop
# is constructed and the persona registers as a tick scheduler), which is why the
# bump fires only when a convene timer is actually armed, and never in reverse.
_MIN_SCHEDULER_LEVEL = "semi-autonomous"

# The legacy tick id/kind the shipped ``TickScheduler`` registers
# (``ScheduledWake(timer_id="legacy_tick", callback_kind="tick")`` — mirrors
# ``agents.tick._LEGACY_TIMER_ID``, pinned by the writer test). Reusing them makes a
# carried-forward entry fire the SAME wake the synthesised legacy timer would have,
# so ``TickScheduler._handle_scheduled_wake`` routes it down the identical tick path
# at the identical cadence. It is NOT identical WIRING: the synthesised timer is
# registered inside ``TickScheduler.start()`` and never reaches
# ``init_persona_timers``, so it has no ``scheduled_wakes`` cache row; an explicit
# entry does, and therefore also picks up the saved-anchor → ``initial_delay``
# first-fire clamp across restarts. Benign (the clamp degrades to a
# fresh-first-fire-shaped delay), but it is a behaviour delta, not a no-op — the
# carry-forward preserves the heartbeat's cadence, not the cache-row absence.
_LEGACY_TICK_ID = "legacy_tick"
_LEGACY_TICK_KIND = "tick"

# The interval the legacy tick fires at when ``tick_interval_seconds`` is unset —
# mirrors ``autonomy.get("tick_interval_seconds", 60)`` in server_persona.py (and the
# schema's ``default: 60``), so a carried-forward tick keeps the exact cadence the
# persona ran at.
_DEFAULT_TICK_INTERVAL_SECONDS = 60

# The busy-loop floor an ``autonomy.timers`` entry must clear: the agent schema's
# ``interval_seconds`` ``minimum: 1.0`` and ``EventLoop._MIN_INTERVAL`` (which raises
# on a sub-floor interval, aborting persona init). The producer only ever derives a
# positive interval, so a spec below the floor is a caller bug — rejected HERE, at
# config-write time, rather than at the convener's next boot.
_MIN_INTERVAL_SECONDS = 1


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
    """Replace the entry sharing ``entry``'s id, else append — so a repeated spec
    refreshes its interval in place instead of double-arming the same channel.

    A *previously written* convene entry never reaches the replace branch: the
    reconcile filter in :func:`merge_convene_timers` drops every convene-kind entry
    before this runs, so a re-applied channel is re-appended, not replaced. What the
    branch actually guards is a ``specs`` iterable that names the same channel twice.
    A collision against a surviving NON-convene id is rejected before we get here —
    silently rewriting an operator's timer (kind included) is not the writer's call.
    """
    for i, existing in enumerate(timers):
        if existing.get("id") == entry["id"]:
            timers[i] = entry
            return
    timers.append(entry)


def merge_convene_timers(
    autonomy: Mapping[str, Any] | None,
    specs: Iterable[ConveneSpec],
) -> dict[str, Any]:
    """Return ``autonomy`` reconciled to exactly the convene timers ``specs`` imply.

    Applies the level bump and tick carry-forward contracts (see the module
    docstring). ``specs`` must be the convener's COMPLETE standing-channel set (the
    producer hands the writer that set, grouped by ``ConvenerID``): the convene-kind
    timers in the result are made to match ``specs`` exactly — a channel dropped from
    ``specs`` (disarmed/deleted) has its convene timer removed, not left armed. Any
    non-convene timer (legacy tick, reflection, …) is preserved untouched. Pure:
    ``autonomy`` and its ``timers`` entries are copied, never mutated.

    An empty ``specs`` on a convener with no convene timer to drop returns the block
    unchanged — no level bump, no ``timers`` key — because that is what the caller
    passes for every persona that convenes nothing (module docstring, "Nothing to
    arm").

    Raises :class:`ValueError` on the three shapes that would otherwise fail at the
    convener's next boot: a non-group ``channel_id`` (a standing channel is
    group-only, so it has no encodable timer id), an ``interval_seconds`` below
    :data:`_MIN_INTERVAL_SECONDS` (the schema + ``EventLoop`` busy-loop floor), and a
    derived timer id already taken by a non-convene timer (whose entry the writer
    must not silently clobber).
    """
    spec_list = list(specs)
    src = dict(autonomy) if autonomy else {}
    level = src.get("level", _DEFAULT_LEVEL)
    was_scheduling = level in _SCHEDULER_LEVELS
    # ``timers`` present-but-``None`` (``timers:`` with no value) is the LEGACY
    # path, NOT the timers path: ``server_persona`` does ``timers = autonomy.get(
    # "timers")`` then ``register_legacy_timer = timers is None``, so a ``None``
    # value leaves ``register_legacy_timer`` TRUE (legacy tick live) and skips
    # ``init_persona_timers`` entirely — behaving exactly like a wholly absent key.
    # Only a present LIST (``[]`` or populated) is the timers path
    # (``register_legacy_timer`` already ``False``). ``is not None`` — deliberately
    # NOT ``"timers" in src`` — is what draws that line: a membership test would
    # fold present-``None`` into the timers path and so DROP the very heartbeat the
    # carry-forward below exists to preserve.
    had_timers_block = src.get("timers") is not None

    # Reconcile, not merely append: this writer is authoritative over the
    # convener's convene-kind timers — its caller passes the FULL set of standing
    # channels this convener drives (the producer's ``StandingConveneTimers``
    # grouped by ``ConvenerID``). So a pre-existing convene timer whose channel is
    # no longer standing (disarmed/deleted → absent from ``specs``) is DROPPED here
    # rather than left firing a wake every interval into a channel that can only
    # 409-decline it. Non-convene entries (the legacy tick, reflection,
    # memory_consolidation, …) are preserved verbatim — the writer owns the convene
    # kind alone. Each channel still in ``specs`` is re-derived below, so a surviving
    # channel's interval refreshes rather than duplicating.
    timers: list[dict[str, Any]] = [
        dict(t)
        for t in (src.get("timers") or [])
        if t.get("kind") != STANDING_CONVENE_KIND
    ]

    # Tick carry-forward: only a convener that was already scheduling with no
    # timers block has a live legacy heartbeat to preserve. A just-bumped reactive
    # convener had none; an already-timers convener already made its tick explicit.
    # Also gated on ``spec_list``: the heartbeat is only at risk because writing a
    # timers block flips ``register_legacy_timer`` off, and with nothing to arm we
    # write no block (see the ``spec_list or had_timers_block`` guard below), so
    # materializing the tick would be a gratuitous config diff — and one that hands
    # the entry a ``scheduled_wakes`` cache row it did not have (see _LEGACY_TICK_ID).
    if spec_list and was_scheduling and not had_timers_block:
        interval = src.get("tick_interval_seconds", _DEFAULT_TICK_INTERVAL_SECONDS)
        _upsert_by_id(
            timers,
            {
                "id": _LEGACY_TICK_ID,
                "interval_seconds": interval,
                "kind": _LEGACY_TICK_KIND,
            },
        )

    # Every id surviving the reconcile filter (plus any carried-forward tick) belongs
    # to a timer kind this writer does NOT own. A convene id colliding with one is an
    # operator config bug — ``parse_standing_convene_timer_id``'s docstring explicitly
    # contemplates operator-named ``convene-*`` non-convene timers — so refuse rather
    # than overwrite: only one entry can hold an id, and it is not the writer's call
    # which. Computed BEFORE the loop so a repeated spec still refreshes in place.
    reserved_ids = {t.get("id") for t in timers}

    for spec in spec_list:
        timer_id = standing_convene_timer_id(spec.channel_id)
        if timer_id is None:
            raise ValueError(
                f"cannot arm a standing convene timer for non-group channel "
                f"{spec.channel_id!r} (standing channels are group-only)"
            )
        if spec.interval_seconds < _MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"standing convene interval {spec.interval_seconds!r}s for "
                f"{spec.channel_id!r} is below the {_MIN_INTERVAL_SECONDS}s busy-loop "
                f"floor (agent.schema interval_seconds minimum / "
                f"EventLoop._MIN_INTERVAL, which would abort the convener's init)"
            )
        if timer_id in reserved_ids:
            raise ValueError(
                f"convene timer id {timer_id!r} for {spec.channel_id!r} collides with "
                f"an existing non-convene timer; rename that timer before arming the "
                f"channel (the writer will not silently replace it)"
            )
        _upsert_by_id(
            timers,
            {
                "id": timer_id,
                "interval_seconds": spec.interval_seconds,
                "kind": STANDING_CONVENE_KIND,
            },
        )

    # Deterministic timer-id order — a stable round-trip diff. The Go producer's
    # ``StandingConveneTimers`` id-sorts its (convene-only) set; here the convene
    # entries interleave with any preserved non-convene timers under the same key.
    # ``.get("id", "")`` rather than ``t["id"]`` so a malformed pre-existing entry
    # missing its (schema-required) id sorts stably instead of raising.
    timers.sort(key=lambda t: t.get("id", ""))

    merged = dict(src)
    # Write a ``timers`` block only when we armed something, or when one already
    # existed (the disarm/reconcile call, where a stale convene entry still has to be
    # dropped). Introducing an empty block on a persona that convenes nothing would
    # flip its ``register_legacy_timer`` off for no reason.
    if spec_list or had_timers_block:
        merged["timers"] = timers
    # Bump only alongside an armed timer: the level is what BUILDS the scheduler, so
    # raising it on a persona with no convene timer grants an EventLoop (and the
    # COST-warning banner) for a schedule that does not exist. One-way by design —
    # reconciling to zero standing channels leaves the level where it is.
    if spec_list and not was_scheduling:
        merged["level"] = _MIN_SCHEDULER_LEVEL
    return merged
