"""Personal-tier recall latency harness (RFC 0029 Phase 1 — §Test Strategy).

Measures the p99/p50 latency of the personal-tier recall path —
:meth:`agents.memory.store.MemoryStore.retrieve_relevant` (the
RFC 0029 §C ``recall_episodes`` personal-tier read) — against a fixed
synthetic corpus on an in-memory database.

Gate call-path (RFC 0029 Phase 1 PR 4 decision)
-----------------------------------------------
The harness times the ``MemoryStore.retrieve_relevant`` facade method,
and the PR 5 regression gate protects *that* method — **not** the raw
``EpisodicMemory`` tier the persona runtime currently drives directly.
Two reasons pin this choice:

1. The gate's job (RFC 0029 §Test Strategy) is to catch a v0.4.0
   Phase 2/3 regression when the personal tier is swapped onto the
   Postgres society backend.  That swap happens *behind the facade*, so
   the facade method is the seam the gate must measure — the raw-tier
   path is not where the backend changes.
2. RFC 0029 Goal 1 converges every caller onto the facade; the persona
   runtime's raw-tier recall is a not-yet-migrated call site, not a
   second supported path.  ``retrieve_relevant`` is the canonical
   personal-tier read the RFC commits to.

The facade method delegates straight to ``EpisodicMemory``, so it adds
only a constant delegation overhead over the raw-tier path — choosing
the facade does not distort the measurement.

Measurement noise
-----------------
p99 over an in-memory SQLite corpus is dominated by cold-start cost —
FTS5 query-plan compilation, a cold page cache, allocator warm-up — on
the first handful of recalls.  :func:`measure_recall_p99` therefore runs
``warmup`` un-timed recalls (default :data:`DEFAULT_WARMUP`) before the
timed window, so the reported p99/p50 reflect steady-state recall rather
than process start-up.  p50 is emitted alongside p99
(``recall_episodes_p50_ms``); the PR 5 gate should track **both** — p99
over a shared CI runner is noisy, and a p50 co-gate catches a real
regression the noisier p99 might flake on or mask.

Phase-1 status — **informational only**.  RFC 0029 Phase 1 is a pure
refactor, so this harness ships and *runs* in PR 3 but does not gate:
there is no baseline to compare against until Phase 1 has merged.
RFC 0029 Phase 1 PR 5 captures ``tests/perf/baselines/personal_tier_latency.json``
from the post-merge number and flips this harness into an enforcing CI
gate (fail on >20% regression) — the legitimate post-rename
personal-tier recall cost reference point.

Run standalone::

    python tests/perf/personal_tier_latency.py
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Standalone runs (``python tests/perf/personal_tier_latency.py``) need the
# repo root on ``sys.path`` so ``import agents`` resolves.  Under pytest the
# harness is loaded with ``agents`` already importable, so this is a no-op.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.memory.store import MemoryStore  # noqa: E402 — see path bootstrap above

#: Default corpus / sampling sizes.  Large enough that FTS5 recall does
#: real ranking work, small enough to run in well under a second.
DEFAULT_CORPUS_SIZE = 500
DEFAULT_ITERATIONS = 500

#: Un-timed recalls run before the timed window.  Absorbs FTS5 query-plan
#: compilation, page-cache warm-up and allocator start-up so the reported
#: p99/p50 measure steady-state recall — see the "Measurement noise" note
#: in the module docstring.
DEFAULT_WARMUP = 50

#: The fixed recall query the harness times.  Chosen to overlap the
#: synthetic corpus topics so recall returns ranked rows, not an empty set.
RECALL_QUERY = "project deadline planning"

_CORPUS_TOPICS = (
    "the project deadline and the release plan",
    "a design review for the planning service",
    "budget planning for the next quarter",
    "the team standup and sprint planning notes",
    "an incident retro and follow-up actions",
)


def _percentile(samples: list[float], pct: float) -> float:
    """Return the *pct* percentile of *samples* (nearest-rank, sorted copy)."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


async def measure_recall_p99(
    *,
    corpus_size: int = DEFAULT_CORPUS_SIZE,
    iterations: int = DEFAULT_ITERATIONS,
    warmup: int = DEFAULT_WARMUP,
) -> dict[str, object]:
    """Measure ``MemoryStore.retrieve_relevant`` p99/p50 latency.

    Builds an in-memory single-agent :class:`MemoryStore`, seeds it with
    *corpus_size* synthetic observations, runs *warmup* un-timed recalls
    to absorb cold-start cost, then times *iterations* recalls of
    :data:`RECALL_QUERY`.  Returns a JSON-serialisable result dict shaped
    for the PR 5 baseline file: ``recall_episodes_p99_ms`` is the gated
    metric, and ``warmup`` records how the number was measured.
    """
    store = MemoryStore(agent_id="perf-harness", db_path=":memory:")
    await store.initialize()
    try:
        for i in range(corpus_size):
            topic = _CORPUS_TOPICS[i % len(_CORPUS_TOPICS)]
            await store.store_observation(
                f"observation {i}: the team discussed {topic}",
                importance=0.5,
                tags=("perf-harness",),
            )
        # Warm-up: these recalls run but their latencies are discarded, so
        # the timed window below measures steady-state recall rather than
        # FTS5 query-plan compilation / page-cache / allocator cold start.
        for _ in range(warmup):
            await store.retrieve_relevant(RECALL_QUERY, limit=10)
        latencies_ms: list[float] = []
        for _ in range(iterations):
            start = time.perf_counter()
            await store.retrieve_relevant(RECALL_QUERY, limit=10)
            latencies_ms.append((time.perf_counter() - start) * 1000.0)
    finally:
        await store.close()

    return {
        "recall_episodes_p99_ms": round(_percentile(latencies_ms, 99.0), 4),
        "recall_episodes_p50_ms": round(_percentile(latencies_ms, 50.0), 4),
        "corpus_size": corpus_size,
        "iterations": iterations,
        "warmup": warmup,
        "query": RECALL_QUERY,
        "captured_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    """Run the harness and print the result dict as one JSON line."""
    result = asyncio.run(measure_recall_p99())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
