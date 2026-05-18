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

Regression gate
---------------
:func:`evaluate_gate` co-checks p99 and p50 against the checked-in
baseline (``tests/perf/baselines/personal_tier_latency.json``) and fails
on a >20% regression (:data:`DEFAULT_REGRESSION_TOLERANCE`).  :func:`main`
runs the gate on every CI invocation.

The gate is **informational-only until a baseline is committed**.  RFC
0029 Phase 1 is a pure refactor, so the legitimate baseline is the
*post-Phase-1-merge* number — and a CI gate must be baselined on the
environment it runs on, so the baseline is captured on a CI runner by
the maintainer-triggered ``perf-baseline-capture`` workflow
(``workflow_dispatch``), not hand-run on a developer machine (RFC 0029
§Test Strategy).  Until that workflow lands the file, :func:`load_baseline`
returns ``None`` and :func:`main` exits 0 after printing the measurement.

Run standalone::

    python tests/perf/personal_tier_latency.py                  # measure + gate
    python tests/perf/personal_tier_latency.py --capture-baseline PATH
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
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

#: Maximum tolerated regression of a gated metric over its baseline before
#: the gate fails the build — >20% per RFC 0029 §Test Strategy.
DEFAULT_REGRESSION_TOLERANCE = 0.20

#: Metrics the gate co-checks.  p99 is the headline budget; p50 is gated
#: alongside it (RFC 0029 Phase 1 PR 4 review) — p99 over a shared CI
#: runner is noisy, and a p50 co-gate catches a real regression the
#: noisier p99 might flake on or mask.
GATED_METRICS = ("recall_episodes_p99_ms", "recall_episodes_p50_ms")

#: Checked-in baseline the gate compares against.  Captured on a CI runner
#: post-Phase-1-merge by the ``perf-baseline-capture`` workflow; absent
#: until then, in which case the gate runs informational-only.
_BASELINE_PATH = (
    _REPO_ROOT / "tests" / "perf" / "baselines" / "personal_tier_latency.json"
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
    metric; ``warmup`` and ``sample_count`` record how the number was
    measured — warm-up applied, and the size of the timed sample set the
    percentiles were computed over (``len`` of the timed latencies, with
    the warm-up recalls excluded).
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
        # Size of the timed sample set the percentiles were computed over.
        # Derived from the timed loop alone, so it equals ``iterations``
        # and *excludes* the ``warmup`` recalls — the pin that warm-up
        # latencies never enter the p99/p50.
        "sample_count": len(latencies_ms),
        "query": RECALL_QUERY,
        "captured_at": datetime.now(UTC).isoformat(),
    }


@dataclass(frozen=True)
class MetricRegression:
    """One gated metric that exceeded its baseline-derived limit."""

    metric: str
    baseline_ms: float
    measured_ms: float
    limit_ms: float


@dataclass(frozen=True)
class GateVerdict:
    """Outcome of :func:`evaluate_gate` — pass/fail plus the regressions."""

    passed: bool
    tolerance: float
    regressions: tuple[MetricRegression, ...]


def load_baseline(path: Path = _BASELINE_PATH) -> dict[str, object] | None:
    """Return the checked-in perf baseline, or ``None`` when none exists.

    A missing baseline is an expected, non-error state: RFC 0029 Phase 1
    is a pure refactor, so the legitimate baseline is captured *after* the
    facade promotion has merged — by the ``perf-baseline-capture``
    workflow — and the gate runs informational-only until it lands.
    """
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        loaded = json.load(fh)
    if not isinstance(loaded, dict):
        raise TypeError(f"baseline {path} is not a JSON object: {loaded!r}")
    return loaded


def _metric_ms(result: dict[str, object], metric: str) -> float:
    """Extract a numeric metric from a measured / baseline result dict."""
    value = result[metric]
    if not isinstance(value, (int, float)):
        raise TypeError(f"{metric} is not numeric: {value!r}")
    return float(value)


def evaluate_gate(
    measured: dict[str, object],
    baseline: dict[str, object],
    *,
    tolerance: float = DEFAULT_REGRESSION_TOLERANCE,
) -> GateVerdict:
    """Compare *measured* against *baseline*; report every regressed metric.

    A gated metric regresses when its measured value is *strictly* greater
    than ``baseline * (1 + tolerance)`` — a metric pinned exactly to the
    limit is the accepted ceiling, not a regression.  Both
    :data:`GATED_METRICS` are co-checked and the gate fails if *either*
    regresses; the returned :class:`GateVerdict` carries one
    :class:`MetricRegression` per failed metric.
    """
    regressions: list[MetricRegression] = []
    for metric in GATED_METRICS:
        baseline_ms = _metric_ms(baseline, metric)
        measured_ms = _metric_ms(measured, metric)
        limit_ms = baseline_ms * (1.0 + tolerance)
        if measured_ms > limit_ms:
            regressions.append(
                MetricRegression(
                    metric=metric,
                    baseline_ms=baseline_ms,
                    measured_ms=measured_ms,
                    limit_ms=round(limit_ms, 4),
                )
            )
    return GateVerdict(
        passed=not regressions,
        tolerance=tolerance,
        regressions=tuple(regressions),
    )


def _captured_commit() -> str:
    """Return the current commit SHA for the baseline's provenance field.

    Prefers ``GITHUB_SHA`` (set on CI) and falls back to ``git rev-parse``;
    ``"unknown"`` if neither resolves, so a capture is never failed by a
    missing provenance string alone.
    """
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _capture_baseline(path: Path) -> int:
    """Measure and write the perf baseline to *path*; return exit code 0.

    Used by the ``perf-baseline-capture`` workflow.  The gate is *not*
    evaluated here, so re-capturing after a deliberate, accepted slowdown
    is never blocked by the previous baseline.
    """
    result = asyncio.run(measure_recall_p99())
    result["captured_commit"] = _captured_commit()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    print(f"perf gate: baseline written to {path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the harness; enforce the regression gate when a baseline exists.

    Returns a process exit code: ``0`` when the gate passes — or runs
    informational-only because no baseline is committed yet — and ``1`` on
    a regression.
    """
    parser = argparse.ArgumentParser(
        description="Personal-tier recall latency gate (RFC 0029 §Test Strategy).",
    )
    parser.add_argument(
        "--capture-baseline",
        metavar="PATH",
        type=Path,
        help="measure and write the baseline JSON to PATH, then exit without "
        "evaluating the gate (used by the perf-baseline-capture workflow)",
    )
    args = parser.parse_args(argv)

    if args.capture_baseline is not None:
        return _capture_baseline(args.capture_baseline)

    result = asyncio.run(measure_recall_p99())
    print(json.dumps(result))

    baseline = load_baseline()
    if baseline is None:
        print(
            "perf gate: informational only — no baseline at "
            f"{_BASELINE_PATH.relative_to(_REPO_ROOT)} yet (captured "
            "post-Phase-1-merge by the perf-baseline-capture workflow)",
            file=sys.stderr,
        )
        return 0

    verdict = evaluate_gate(result, baseline)
    if verdict.passed:
        print("perf gate: PASS", file=sys.stderr)
        return 0
    pct = round(verdict.tolerance * 100)
    for reg in verdict.regressions:
        print(
            f"perf gate: FAIL — {reg.metric} {reg.measured_ms} ms exceeds "
            f"baseline {reg.baseline_ms} ms +{pct}% limit {reg.limit_ms} ms",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
