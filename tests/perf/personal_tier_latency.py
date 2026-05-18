"""Personal-tier recall latency harness (RFC 0029 Phase 1 — §Test Strategy).

Measures the p99 latency of the personal-tier recall path —
:meth:`agents.memory.store.MemoryStore.retrieve_relevant` (the
RFC 0029 §C ``recall_episodes`` personal-tier read) — against a fixed
synthetic corpus on an in-memory database.

The harness times the ``MemoryStore`` facade recall method.  The persona
runtime currently drives the raw ``EpisodicMemory`` tier directly rather
than holding a ``MemoryStore``; the facade method is the metric here
because RFC 0029 is converging callers onto it.  Whether the PR 5 gate
should track the facade method or the raw-tier call path the runtime
exercises is a PR 4/5 follow-up (see the RFC 0029 PR plan).

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
from datetime import datetime, timezone
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
) -> dict[str, object]:
    """Measure ``MemoryStore.retrieve_relevant`` p99 latency.

    Builds an in-memory single-agent :class:`MemoryStore`, seeds it with
    *corpus_size* synthetic observations, then times *iterations* recalls
    of :data:`RECALL_QUERY`.  Returns a JSON-serialisable result dict
    shaped for the PR 5 baseline file
    (``recall_episodes_p99_ms`` is the gated metric).
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
        "query": RECALL_QUERY,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    """Run the harness and print the result dict as one JSON line."""
    result = asyncio.run(measure_recall_p99())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
