"""Persona-agent timer & scheduled-wakes-cache wiring helpers — RFC 0024.

Split out of :mod:`agents.server_persona` for file-size review-
friendliness (PR 2 review item 2 was the parallel split for
``event_loop.py``; PR 2.1's cache-wiring additions trigger the same
split here).  Public API: :func:`init_persona_timers` and
:func:`summarize_autonomy_cadence` — called from
:func:`agents.server_persona.initialize_persona_agents`.

The helpers in this module own:

* **Config → cache rows** — :func:`_build_scheduled_wake_rows` (rounding
  rule for ``interval_ms``, anchor-preserve-or-reset rule for
  ``next_fire_at_ms``).
* **EventLoop registration** — :func:`_register_configured_timers`
  (the saved-anchor → ``initial_delay`` clamp).
* **Cache lifecycle inside init** — :func:`init_persona_timers`
  (open, rebuild, register, cleanup on failure).
"""

from __future__ import annotations

import logging
import time

from .base import BaseAgent
from .event_loop import EventLoop
from .memory.scheduled_wakes import ScheduledWakeRow, ScheduledWakesCache
from .tick import TickScheduler

logger = logging.getLogger("Persatrix.agent.server_persona")

__all__ = ["init_persona_timers", "summarize_autonomy_cadence"]


def summarize_autonomy_cadence(timers: list[dict] | None, interval: int) -> str:
    """Cadence summary for COST/Started logs (RFC 0024 PR 2).

    ``interval_seconds`` is rendered with ``{:g}`` so an integer-valued
    float (``60.0``) reads identically to its integer spelling (``60``)
    while a genuinely fractional value (``60.5``) keeps its precision.
    The schema declares ``interval_seconds`` as ``type: number``, so both
    spellings are legal config; normalising here keeps the COST log
    consistent regardless of how the operator wrote the value
    (PR 2 review (6)).
    """
    if timers is None:
        return f"tick_interval={interval}s"
    body = ", ".join(
        f"{t['id']}@{float(t['interval_seconds']):g}s" for t in timers
    )
    return f"timers=[{body}]"


def _build_scheduled_wake_rows(
    timers: list[dict],
    *,
    now_ms: int,
    saved_anchors_ms: dict[str, int],
) -> list[ScheduledWakeRow]:
    """Convert ``autonomy.timers`` config rows into cache rows.

    Scope decision (RFC 0024 PR 2.1): ``interval_ms = round(interval_seconds
    * 1000)``.  ``round()`` keeps integer-valued ``interval_seconds`` exact
    (``30 → 30000``) and rounds fractional values to nearest millisecond
    (``1.501 → 1501``).  ``int(interval_seconds * 1000)`` was rejected
    because it truncates ``1.5001 → 1500`` which silently widens cadence
    sub-millisecond at every config touch.

    ``next_fire_at_ms`` reflects the planned monotonic anchor of the
    *next* fire so a subsequent restart can honour it: a saved anchor
    still in the future is preserved; everything else (past anchors,
    brand-new timers) is reset to ``now + interval_ms``.
    """
    rows: list[ScheduledWakeRow] = []
    for cfg in timers:
        interval_seconds = float(cfg["interval_seconds"])
        jitter_max_seconds = float(cfg.get("jitter_max_seconds", 0.0))
        interval_ms = round(interval_seconds * 1000)
        jitter_ms = round(jitter_max_seconds * 1000)
        saved = saved_anchors_ms.get(cfg["id"])
        if saved is not None and saved > now_ms:
            next_fire_at_ms = saved
        else:
            next_fire_at_ms = now_ms + interval_ms
        rows.append(ScheduledWakeRow(
            timer_id=cfg["id"],
            kind=cfg["kind"],
            interval_ms=interval_ms,
            jitter_ms=jitter_ms,
            next_fire_at_ms=next_fire_at_ms,
            source="config",
        ))
    return rows


async def _register_configured_timers(
    scheduler: TickScheduler,
    timers: list[dict],
    agent_id: str,
    *,
    saved_anchors_ms: dict[str, int] | None = None,
    now_ms: int | None = None,
) -> None:
    """Register each ``autonomy.timers`` entry on ``scheduler.event_loop``.

    On failure, stops the (already-started) scheduler then re-raises the
    original error; a failure inside ``stop()`` is logged but must not
    replace the active exception (operators need the YAML diagnostic).
    Pinned by the two ``test_partial_register_failure_*`` wiring tests.

    ``saved_anchors_ms`` (RFC 0024 PR 2.1): per-timer
    :attr:`ScheduledWakeRow.next_fire_at_ms` from the cache.  Anchors in
    the future pass as ``initial_delay`` so a restart mid-jitter-window
    resumes the saved monotonic anchor (clamped to
    ``[_MIN_INTERVAL, interval + jitter_max]`` so a stale anchor cannot
    delay first-fire arbitrarily).  Past anchors and brand-new timers
    fall through to the default first-fire shape.  ``now_ms`` is
    captured once at the caller so every timer sees a consistent "now".
    """
    saved = saved_anchors_ms or {}
    try:
        for cfg in timers:
            interval = float(cfg["interval_seconds"])
            jitter_max = float(cfg.get("jitter_max_seconds", 0.0))
            initial_delay: float | None = None
            saved_anchor = saved.get(cfg["id"])
            # ``init_persona_timers`` invariant: ``saved_anchors_ms`` is
            # populated iff ``now_ms`` was captured; so at runtime
            # ``saved_anchor is not None`` already implies
            # ``now_ms is not None``.  The second check is kept for mypy
            # type-narrowing — without it the subtraction below sees
            # ``now_ms: int | None``.  (RFC 0024 PR 2.1 review follow-up.)
            if (
                saved_anchor is not None
                and now_ms is not None
                and saved_anchor > now_ms
            ):
                raw = (saved_anchor - now_ms) / 1000.0
                upper = interval + jitter_max
                initial_delay = max(EventLoop._MIN_INTERVAL, min(raw, upper))
            scheduler.event_loop.register_timer(
                timer_id=cfg["id"], callback_kind=cfg["kind"],
                interval=interval,
                jitter_max=jitter_max,
                initial_delay=initial_delay,
            )
    except Exception:
        try:
            await scheduler.stop()
        except Exception:
            logger.exception(
                "Agent %s: scheduler.stop() failed during partial-init "
                "cleanup; original timer-registration error follows.", agent_id,
            )
        raise


async def init_persona_timers(
    scheduler: TickScheduler,
    agent: BaseAgent,
    agent_id: str,
    *,
    timers: list[dict],
    scheduled_wakes_caches: dict[str, ScheduledWakesCache] | None,
) -> None:
    """Open the scheduled-wakes cache (when the caller provides the dict),
    rebuild it from ``timers``, register every timer on the event loop,
    and store the cache for later shutdown.

    Partial-init cleanup contract — both failure modes converge:

    * ``register_timer`` failure: ``_register_configured_timers`` stops
      the scheduler internally; this function closes the freshly-opened
      cache before re-raising.
    * Cache-setup failure (``initialize`` / ``list_timers`` /
      ``rebuild_from_config``): this function closes the cache (no-op
      if ``_conn`` was never set) *and* stops the scheduler so the
      caller does not observe a running scheduler whose entry never
      reaches ``tick_schedulers``.  Pinned by
      ``test_scheduled_wakes_cache_wiring.TestCacheLifecycleOnSetupFailure``.

    Why both branches stop the scheduler: ``initialize_persona_agents``
    calls ``scheduler.start()`` *before* invoking this function (see
    the call site in :func:`agents.server_persona.initialize_persona_agents`).
    A failure here therefore leaves an orphan supervisor task unless we
    actively stop it — the caller's ``tick_schedulers`` dict won't
    contain it, so ``AgentServer.stop()`` can't.

    Either cleanup error (cache or scheduler) is surfaced via
    ``logger.exception`` but must not mask the original exception —
    operators need the actionable root cause, not the cleanup chain.

    See :mod:`agents.memory.scheduled_wakes` module docstring for the
    ``next_fire_at_ms`` clock contract — monotonic on write, bounded
    by the loader's ``[_MIN_INTERVAL, interval + jitter_max]`` clamp
    on read so a cross-process-meaningless anchor (post-reboot)
    degrades to a fresh-first-fire-shaped delay rather than firing
    immediately or at some arbitrary far-future time.
    """
    saved_anchors_ms: dict[str, int] = {}
    now_ms: int | None = None
    cache: ScheduledWakesCache | None = None
    if scheduled_wakes_caches is not None:
        # RFC 0024 PR 2.1: read existing rows (saved anchors), rebuild
        # from config (orphan cleanup + interval refresh), pass anchors
        # downstream.  ``next_fire_at_ms`` uses
        # ``time.monotonic_ns() // 1_000_000`` per RFC 0024 §C.
        memory_cfg = agent.config.get("memory") or {}
        db_path = memory_cfg.get("db_path", "data/memory.db")
        cache = ScheduledWakesCache(db_path=db_path, agent_id=agent_id)
        try:
            await cache.initialize()
            existing = await cache.list_timers()
            saved_anchors_ms = {
                row.timer_id: row.next_fire_at_ms
                for row in existing
                if row.next_fire_at_ms > 0
            }
            now_ms = time.monotonic_ns() // 1_000_000
            rebuilt_rows = _build_scheduled_wake_rows(
                timers, now_ms=now_ms, saved_anchors_ms=saved_anchors_ms,
            )
            await cache.rebuild_from_config(rebuilt_rows)
        except Exception:
            # Cache-setup failed before ``_register_configured_timers``
            # ran, so the scheduler's own self-stop path never fires.
            # Mirror the register-failure contract here: close the
            # (possibly half-opened) cache and stop the started
            # scheduler before the original error propagates.
            try:
                await cache.close()
            except Exception:
                logger.exception(
                    "Agent %s: cache.close() failed during cache-setup "
                    "cleanup; original cache-setup error follows.",
                    agent_id,
                )
            try:
                await scheduler.stop()
            except Exception:
                logger.exception(
                    "Agent %s: scheduler.stop() failed during cache-setup "
                    "cleanup; original cache-setup error follows.",
                    agent_id,
                )
            raise
    try:
        await _register_configured_timers(
            scheduler, timers, agent_id,
            saved_anchors_ms=saved_anchors_ms,
            now_ms=now_ms,
        )
    except Exception:
        if cache is not None:
            try:
                await cache.close()
            except Exception:
                logger.exception(
                    "Agent %s: cache.close() failed during partial-init "
                    "cleanup; original timer-registration error follows.",
                    agent_id,
                )
        raise
    if cache is not None and scheduled_wakes_caches is not None:
        scheduled_wakes_caches[agent_id] = cache
