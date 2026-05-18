"""RFC 0029 Phase 1 PR 5 — perf-gate enforcement (TDD pins).

PR 5 flips ``tests/perf/personal_tier_latency.py`` from an informational
harness into an enforcing regression gate (RFC 0029 §Test Strategy — fail
the build on a >20% regression off the Phase 1 post-merge baseline). These
tests pin the gate's behaviour:

- :func:`evaluate_gate` — compares a measured result against a baseline and
  reports every regressed metric. p99 *and* p50 are co-checked (RFC 0029
  Phase 1 PR 4 review: p99 over a shared CI runner is noisy, so a p50
  co-gate catches a regression the noisier p99 might flake on or mask).
- :func:`load_baseline` — reads the checked-in baseline JSON, returning
  ``None`` when no baseline has been committed yet (the expected state
  until the maintainer-triggered capture workflow lands one).
- :func:`main` — the CI gate entrypoint. Its exit code is the contract CI
  depends on (0 informational / 0 pass / 1 regression); the
  environment-dependent measurement is stubbed so only the verdict logic
  is pinned, never a latency number.

The harness is loaded by file path — it lives under ``tests/perf/`` and is
not an importable package — mirroring the PR 4 pin in
``test_personal_tier_latency_harness.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


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
    # Register before exec_module: dataclass (and other class-creation)
    # machinery resolves ``cls.__module__`` through ``sys.modules`` while
    # the class body runs, so a by-path load that skips this step crashes
    # on the harness's ``@dataclass`` gate-verdict types.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: A baseline both gated metrics are measured against in the tests below.
#: p99 limit at the default 20% tolerance is 3.6 ms; p50 limit is 1.2 ms.
_BASELINE = {"recall_episodes_p99_ms": 3.0, "recall_episodes_p50_ms": 1.0}


# ─── the gate constants ───────────────────────────────────────


def test_default_regression_tolerance_is_twenty_percent() -> None:
    """RFC 0029 §Test Strategy fixes the gate at >20% regression."""
    harness = _load_perf_harness()
    assert harness.DEFAULT_REGRESSION_TOLERANCE == 0.20


def test_gated_metrics_co_check_p99_and_p50() -> None:
    """The gate co-checks p50 alongside p99 (PR 4 review) — both metric
    keys the harness emits in its result dict are gated.
    """
    harness = _load_perf_harness()
    assert set(harness.GATED_METRICS) == {
        "recall_episodes_p99_ms",
        "recall_episodes_p50_ms",
    }


# ─── evaluate_gate: pass / fail verdicts ──────────────────────


def test_gate_passes_when_both_metrics_within_tolerance() -> None:
    """A measured result slightly above baseline but inside the 20% band
    is not a regression — the gate passes with no regressions reported.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 3.5, "recall_episodes_p50_ms": 1.1}
    verdict = harness.evaluate_gate(measured, _BASELINE)
    assert verdict.passed is True
    assert verdict.regressions == ()


def test_gate_passes_when_faster_than_baseline() -> None:
    """A measured result below the baseline is an improvement, never a
    regression.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 2.0, "recall_episodes_p50_ms": 0.5}
    verdict = harness.evaluate_gate(measured, _BASELINE)
    assert verdict.passed is True
    assert verdict.regressions == ()


def test_gate_fails_on_p99_regression() -> None:
    """A p99 above baseline * 1.20 fails the gate and is named in the
    regression list.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 4.0, "recall_episodes_p50_ms": 1.0}
    verdict = harness.evaluate_gate(measured, _BASELINE)
    assert verdict.passed is False
    assert [r.metric for r in verdict.regressions] == ["recall_episodes_p99_ms"]


def test_gate_fails_on_p50_regression_even_when_p99_holds() -> None:
    """p50 is co-gated: a p50 regression fails the build on its own, so a
    real slowdown the noisier p99 happens to absorb is still caught.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 3.0, "recall_episodes_p50_ms": 1.5}
    verdict = harness.evaluate_gate(measured, _BASELINE)
    assert verdict.passed is False
    assert [r.metric for r in verdict.regressions] == ["recall_episodes_p50_ms"]


def test_gate_reports_every_regressed_metric() -> None:
    """When both metrics regress, both are listed — the verdict is not
    short-circuited on the first failure.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 5.0, "recall_episodes_p50_ms": 2.0}
    verdict = harness.evaluate_gate(measured, _BASELINE)
    assert verdict.passed is False
    assert {r.metric for r in verdict.regressions} == {
        "recall_episodes_p99_ms",
        "recall_episodes_p50_ms",
    }


def test_gate_boundary_is_exclusive() -> None:
    """A metric measured exactly at baseline * (1 + tolerance) is the
    accepted ceiling, not a regression — the gate fires on strictly
    greater, so a metric pinned to the boundary does not flake the build.
    """
    harness = _load_perf_harness()
    measured = {
        "recall_episodes_p99_ms": 3.0 * 1.20,  # 3.6 — exactly the limit
        "recall_episodes_p50_ms": 1.0 * 1.20,  # 1.2 — exactly the limit
    }
    verdict = harness.evaluate_gate(measured, _BASELINE)
    assert verdict.passed is True


def test_gate_honours_custom_tolerance() -> None:
    """A tighter tolerance shrinks the accepted band: at tolerance 0.0 any
    increase over baseline is a regression.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 3.1, "recall_episodes_p50_ms": 1.0}
    verdict = harness.evaluate_gate(measured, _BASELINE, tolerance=0.0)
    assert verdict.passed is False
    assert verdict.tolerance == 0.0
    assert [r.metric for r in verdict.regressions] == ["recall_episodes_p99_ms"]


def test_regression_entry_names_baseline_measured_and_limit() -> None:
    """Each regression entry carries the numbers an operator needs to act:
    the baseline, the measured value, and the limit it broke.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 4.0, "recall_episodes_p50_ms": 1.0}
    verdict = harness.evaluate_gate(measured, _BASELINE)
    (entry,) = verdict.regressions
    assert entry.metric == "recall_episodes_p99_ms"
    assert entry.baseline_ms == 3.0
    assert entry.measured_ms == 4.0
    assert entry.limit_ms == 3.6


# ─── load_baseline: missing vs committed ──────────────────────


def test_load_baseline_returns_none_when_file_absent(tmp_path: Path) -> None:
    """No baseline committed yet is the expected pre-capture state — a
    missing file resolves to ``None``, not an error, so the gate can run
    informational-only until the capture workflow lands one.
    """
    harness = _load_perf_harness()
    assert harness.load_baseline(tmp_path / "does-not-exist.json") is None


def test_load_baseline_reads_committed_json(tmp_path: Path) -> None:
    """A committed baseline file is parsed and returned as a dict the
    gate can compare against.
    """
    harness = _load_perf_harness()
    baseline_path = tmp_path / "personal_tier_latency.json"
    baseline_path.write_text(json.dumps(_BASELINE), encoding="utf-8")
    loaded = harness.load_baseline(baseline_path)
    assert loaded == _BASELINE


def test_load_baseline_rejects_non_object_json(tmp_path: Path) -> None:
    """A baseline file holding valid JSON that is not an object (a list, a
    bare scalar) is rejected with ``TypeError`` — the gate needs a metric
    map, and a bare subscript later would fail less legibly.
    """
    harness = _load_perf_harness()
    baseline_path = tmp_path / "personal_tier_latency.json"
    baseline_path.write_text(json.dumps([3.0, 1.0]), encoding="utf-8")
    with pytest.raises(TypeError, match="not a JSON object"):
        harness.load_baseline(baseline_path)


# ─── evaluate_gate: malformed baseline ────────────────────────


def test_gate_raises_actionable_error_when_baseline_omits_a_metric() -> None:
    """A baseline missing a gated metric fails with an error naming the
    keys that *are* present — not a bare subscript ``KeyError`` that names
    only the missing key. A committed baseline can drift from the harness
    (a hand-edit, or a metric rename landing while an old baseline file
    lingers), and the gate should report what it actually found.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 3.0, "recall_episodes_p50_ms": 1.0}
    incomplete = {"recall_episodes_p99_ms": 3.0}  # recall_episodes_p50_ms absent
    with pytest.raises(KeyError, match="present keys"):
        harness.evaluate_gate(measured, incomplete)


def test_gate_raises_on_non_numeric_metric() -> None:
    """A gated metric whose value is not a number (a hand-edited baseline,
    a stringified JSON field) fails with ``TypeError`` naming the metric
    and the dict it came from — never a silent coerce or comparison crash.
    """
    harness = _load_perf_harness()
    measured = {"recall_episodes_p99_ms": 3.0, "recall_episodes_p50_ms": 1.0}
    non_numeric = {"recall_episodes_p99_ms": "3.0", "recall_episodes_p50_ms": 1.0}
    with pytest.raises(TypeError, match="not numeric"):
        harness.evaluate_gate(measured, non_numeric)


# ─── main: exit-code contract ─────────────────────────────────


def _stub_measurement(
    harness: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, float],
) -> None:
    """Replace the live latency measurement with a fixed *result* dict.

    ``main`` is the CI gate entrypoint; the environment-dependent
    measurement is stubbed so the tests pin only the exit-code contract,
    never a latency number.
    """

    async def _fake_measure(**_kwargs: object) -> dict[str, float]:
        return dict(result)

    monkeypatch.setattr(harness, "measure_recall_p99", _fake_measure)


def test_main_runs_informational_when_no_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No committed baseline → the gate runs informational-only: ``main``
    exits 0 even on a measurement that would regress, never failing CI.
    """
    harness = _load_perf_harness()
    _stub_measurement(
        harness,
        monkeypatch,
        {"recall_episodes_p99_ms": 99.0, "recall_episodes_p50_ms": 99.0},
    )
    monkeypatch.setattr(harness, "load_baseline", lambda: None)
    assert harness.main([]) == 0


def test_main_exits_zero_when_gate_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measurement within tolerance of the committed baseline passes the
    gate — ``main`` exits 0.
    """
    harness = _load_perf_harness()
    _stub_measurement(
        harness,
        monkeypatch,
        {"recall_episodes_p99_ms": 3.0, "recall_episodes_p50_ms": 1.0},
    )
    monkeypatch.setattr(harness, "load_baseline", lambda: dict(_BASELINE))
    assert harness.main([]) == 0


def test_main_exits_one_on_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measurement past baseline + tolerance fails the gate — ``main``
    exits 1 so the CI step fails the build. This non-zero exit is the
    contract the whole gate exists to enforce.
    """
    harness = _load_perf_harness()
    _stub_measurement(
        harness,
        monkeypatch,
        {"recall_episodes_p99_ms": 99.0, "recall_episodes_p50_ms": 1.0},
    )
    monkeypatch.setattr(harness, "load_baseline", lambda: dict(_BASELINE))
    assert harness.main([]) == 1


def test_main_capture_baseline_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``--capture-baseline PATH`` measures, writes the baseline JSON
    (creating parent directories), records a ``captured_commit`` provenance
    field, and exits 0 without evaluating the gate — the entrypoint the
    perf-baseline-capture workflow drives.
    """
    harness = _load_perf_harness()
    _stub_measurement(
        harness,
        monkeypatch,
        {"recall_episodes_p99_ms": 3.0, "recall_episodes_p50_ms": 1.0},
    )
    out_path = tmp_path / "baselines" / "personal_tier_latency.json"
    assert harness.main(["--capture-baseline", str(out_path)]) == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["recall_episodes_p99_ms"] == 3.0
    assert written["recall_episodes_p50_ms"] == 1.0
    assert "captured_commit" in written
