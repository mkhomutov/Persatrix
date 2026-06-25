"""RFC 0034 Phase 3 (v0.3.10) — conversation-window instrumentation.

The RFC 0034 conversation window (:mod:`agents.persona_runtime.conversation_window`,
Phases 1–2) reconstructs the LLM ``messages`` array from the channel store every
persona turn, fronted by an in-process fetch cache. Phase 1 deliberately shipped
**no** telemetry ("shipping inert counters now would invite premature dashboard
work" — RFC 0034 PR plan). Phase 3 adds the observability that lets an operator
re-tune the ``max_turns`` / ``max_tokens`` defaults and the LRU cache bound from
real data rather than a guess:

* ``conversation_window.cache_access`` — charted by ``result`` (``hit``/``miss``);
  the cache-hit rate is ``hit / (hit + miss)`` over *consulted* look-ups (a turn
  whose event carries no ``message_id`` never consults the cache, so it charts
  neither — the denominator stays honest).
* ``conversation_window.cache_evictions`` — least-recently-used evictions from the
  bounded cache, so an undersized bound is *visible* (thrashing) rather than
  silently degrading the hit rate.
* ``conversation_window.fetch_duration`` — the wall-clock cost of one real history
  fetch (the cost the cache exists to avoid). Recorded on a successful fetch only;
  a failed fetch is charted by the fallback counter, so timeout latency never skews
  the steady-state cost the defaults are re-tuned against.
* ``conversation_window.fallback`` — charted by ``reason`` (``fetch_failed`` =
  the history fetch raised; ``fetch_none`` = the fetcher returned its own
  best-effort ``None``). This is the silent degrade-to-current-event-only the RFC
  §F risk table flagged as masking a real outage.

Split out of :mod:`agents.observability.metrics` so the parent module stays under
the project's 500-line review cap (see ``scripts/checks/file_size.py``), mirroring
:mod:`._metrics_salience` / :mod:`._metrics_persona_tick` / :mod:`._metrics_wakes`.
Like the RFC 0051 deliberation/reflexion blocks, these instruments live in module
state rather than on :class:`_Instruments` — ``metrics.py`` is at the cap and
cannot gain the class annotations the ``inst.X`` pattern needs. :func:`register`
re-creates them on every ``_Instruments`` construction (every ``init_metrics``),
so they always track the live meter; every ``record_*`` helper is a no-op until
then, so a call site never has to guard.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter

    from .metrics import _Instruments


@dataclass
class _ConversationWindowInstruments:
    """The module-owned RFC 0034 Phase 3 conversation-window instruments."""

    cache_access: Counter
    cache_evictions: Counter
    fetch_duration: Histogram
    fallback: Counter


_instruments: _ConversationWindowInstruments | None = None


def record_cache_access(*, hit: bool) -> None:
    """Record one fetch-cache look-up, charted by ``result`` (``hit``/``miss``).

    Called only on the *consulted* path (the event carries a ``message_id``); a
    no-op until :func:`register` has run. Best-effort — the look-up already
    resolved, so a metric-export hiccup must never propagate and undo the turn."""
    inst = _instruments
    if inst is None:
        return
    with contextlib.suppress(Exception):
        inst.cache_access.add(1, attributes={"result": "hit" if hit else "miss"})


def record_cache_eviction(count: int = 1) -> None:
    """Record ``count`` least-recently-used evictions from the bounded cache.

    A no-op until :func:`register` has run; best-effort for the same reason as
    :func:`record_cache_access`."""
    inst = _instruments
    if inst is None or count <= 0:
        return
    with contextlib.suppress(Exception):
        inst.cache_evictions.add(count)


def record_fetch_duration(duration_ms: float) -> None:
    """Record the wall-clock duration (ms) of one successful history fetch.

    A no-op until :func:`register` has run; best-effort for the same reason as
    :func:`record_cache_access`."""
    inst = _instruments
    if inst is None:
        return
    with contextlib.suppress(Exception):
        inst.fetch_duration.record(duration_ms)


def record_fallback(*, reason: str) -> None:
    """Record one degrade-to-current-event-only, charted by ``reason``.

    ``reason`` is the closed set ``fetch_failed`` / ``fetch_none`` (bounded, so
    safe as a metric dimension). A no-op until :func:`register` has run;
    best-effort for the same reason as :func:`record_cache_access`."""
    inst = _instruments
    if inst is None:
        return
    with contextlib.suppress(Exception):
        inst.fallback.add(1, attributes={"reason": reason})


def register(inst: _Instruments, meter: Meter) -> None:
    """Create the module-owned conversation-window instruments on ``meter``.

    ``inst`` is unused — these instruments are module-owned (see the module
    docstring), not attributes of :class:`_Instruments` — but the signature
    matches the uniform ``mod.register(self, meter)`` call in
    :meth:`_Instruments.__init__` so this module registers in the same loop."""
    global _instruments
    _instruments = _ConversationWindowInstruments(
        cache_access=meter.create_counter(
            name="conversation_window.cache_access",
            unit="{lookup}",
            description=(
                "RFC 0034 conversation-window fetch-cache look-ups. Attribute: "
                "result (hit|miss). Cache-hit rate = hit / (hit + miss); a turn "
                "whose event has no message_id never consults the cache and "
                "charts neither, so the ratio reflects consulted look-ups only."
            ),
        ),
        cache_evictions=meter.create_counter(
            name="conversation_window.cache_evictions",
            unit="{eviction}",
            description=(
                "RFC 0034 conversation-window fetch-cache least-recently-used "
                "evictions. A sustained rate means the LRU bound "
                "(conversation_window._WINDOW_CACHE capacity) is undersized for "
                "the live channel count and the cache is thrashing."
            ),
        ),
        fetch_duration=meter.create_histogram(
            name="conversation_window.fetch_duration",
            unit="ms",
            description=(
                "Wall-clock duration of one RFC 0034 conversation-window history "
                "fetch — the per-turn cost the cache exists to avoid. Recorded on "
                "a successful fetch only; a failed fetch is charted by "
                "conversation_window.fallback so timeout latency does not skew the "
                "steady-state fetch cost the max_turns/max_tokens defaults retune "
                "against."
            ),
        ),
        fallback=meter.create_counter(
            name="conversation_window.fallback",
            unit="{turn}",
            description=(
                "RFC 0034 conversation-window turns that degraded to "
                "current-event-only despite an enabled window on a channel turn. "
                "Attribute: reason (fetch_failed = the history fetch raised; "
                "fetch_none = the fetcher returned its best-effort None). The §F "
                "silent-degradation signal — a sustained rate is a history-endpoint "
                "outage, not the feature working as intended."
            ),
        ),
    )
