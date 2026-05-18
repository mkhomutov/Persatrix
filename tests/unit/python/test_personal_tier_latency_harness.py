"""RFC 0029 Phase 1 PR 4 — perf-harness review follow-ups (TDD pins).

PR 4 addresses the two perf-harness findings from the PR 3 review of
``tests/perf/personal_tier_latency.py``:

- **Measurement noise.** p99 over an in-memory SQLite corpus is dominated
  by cold-start cost — FTS5 query-plan compilation, a cold page cache,
  allocator warm-up — on the first few recalls.  The harness now runs a
  configurable number of *un-timed* ``warmup`` recalls before the timed
  window so the reported p99/p50 reflect steady-state recall, not
  process start-up.  These tests pin the warm-up contract.
- **Gate call-path.** PR 4 pins (in the harness docstring) that the PR 5
  gate protects the ``MemoryStore.retrieve_relevant`` facade method — not
  asserted here; it is a docstring-only decision.

The harness itself is loaded by file path (it lives under ``tests/perf/``
and is not an importable package) — same pattern as the PR 3 pin in
``test_rfc0029_callsite_refactor.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_perf_harness() -> ModuleType:
    """Load ``tests/perf/personal_tier_latency.py`` as a module by path."""
    repo_root = Path(__file__).resolve().parents[3]
    harness_path = repo_root / "tests" / "perf" / "personal_tier_latency.py"
    assert harness_path.is_file(), f"perf harness missing: {harness_path}"
    spec = importlib.util.spec_from_file_location(
        "personal_tier_latency", harness_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── warm-up is reported in the result dict ───────────────────


async def test_measure_recall_p99_reports_warmup_count() -> None:
    """The result dict carries the warm-up count so PR 5's baseline JSON
    records how the captured number was measured, not just the number.
    """
    harness = _load_perf_harness()
    result = await harness.measure_recall_p99(
        corpus_size=16, iterations=5, warmup=3,
    )
    assert result["warmup"] == 3
    # The timed window — and therefore the gated p99 — is the iterations
    # count, unaffected by how many warm-up recalls preceded it.
    assert result["iterations"] == 5


# ─── warm-up recalls run but are excluded from the timed window ─


async def test_warmup_recalls_run_but_are_not_timed(monkeypatch) -> None:
    """Warm-up recalls are *executed* (they have to be, to warm the FTS5
    query plan and page cache) but are not part of the timed sample set.
    """
    harness = _load_perf_harness()
    from agents.memory.store import MemoryStore

    real_retrieve = MemoryStore.retrieve_relevant
    recall_calls = 0

    async def _counting_retrieve(self, *args, **kwargs):  # noqa: ANN001, ANN002
        nonlocal recall_calls
        recall_calls += 1
        return await real_retrieve(self, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, "retrieve_relevant", _counting_retrieve)

    result = await harness.measure_recall_p99(
        corpus_size=16, iterations=5, warmup=3,
    )

    # 3 warm-up + 5 timed recalls all execute against the store ...
    assert recall_calls == 8
    # ... but only the 5 timed recalls are reported as the sample set.
    assert result["iterations"] == 5
    assert result["warmup"] == 3


async def test_zero_warmup_times_every_recall(monkeypatch) -> None:
    """``warmup=0`` reproduces the pre-PR-4 behaviour — every recall is
    timed — so the parameter is a strict, opt-out-able superset.
    """
    harness = _load_perf_harness()
    from agents.memory.store import MemoryStore

    real_retrieve = MemoryStore.retrieve_relevant
    recall_calls = 0

    async def _counting_retrieve(self, *args, **kwargs):  # noqa: ANN001, ANN002
        nonlocal recall_calls
        recall_calls += 1
        return await real_retrieve(self, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, "retrieve_relevant", _counting_retrieve)

    result = await harness.measure_recall_p99(
        corpus_size=16, iterations=6, warmup=0,
    )

    assert recall_calls == 6
    assert result["warmup"] == 0
    assert result["iterations"] == 6


# ─── the harness ships a non-zero default warm-up ─────────────


async def test_harness_defines_a_nonzero_default_warmup() -> None:
    """A plain ``measure_recall_p99()`` (and the standalone ``main()``)
    must absorb cold-start noise without the caller opting in — so the
    default warm-up is a positive count, not zero.
    """
    harness = _load_perf_harness()
    assert isinstance(harness.DEFAULT_WARMUP, int)
    assert harness.DEFAULT_WARMUP > 0

    # Calling without a `warmup` kwarg falls back to DEFAULT_WARMUP.
    result = await harness.measure_recall_p99(corpus_size=16, iterations=5)
    assert result["warmup"] == harness.DEFAULT_WARMUP
