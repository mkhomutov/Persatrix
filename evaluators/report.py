"""RFC 0044 Phase 1 — the structured per-assertion eval report artifact (PR 3).

The runner emits a machine-readable artifact for each replayed recipe (RFC 0044
§F): the per-assertion pass/fail rows plus roll-up counts, keyed by eval id, tier,
and mode. Phase 1 ships the artifact for a human to read; Phase 2 attaches it to
the CI run and gates merge on the ``stable`` tier.

Kept **pure** — it depends only on the report dataclasses
(:class:`~evaluators.eval_set.EvalReport` /
:class:`~evaluators.assertions.AssertionResult`), never the persona runtime — so
the artifact is trivially JSON-safe and unit-testable. ``mode`` is accepted
duck-typed (an :class:`~evaluators.runner.EvalMode` member *or* a plain string) so
this module carries no import back to the runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluators.eval_set import EvalReport


def report_to_dict(
    report: EvalReport,
    *,
    tier: str,
    mode: Any,
    shadow_traces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serialize one recipe's :class:`EvalReport` to a JSON-safe artifact dict.

    ``mode`` may be an ``EvalMode`` member or a string; its ``.value`` is used
    when present so the artifact records the plain mode name (``"replay"``).

    ``shadow_traces`` (RFC 0049 PR 2) are the run's captured L2 cross-room
    shadow records; included under a ``"shadow_traces"`` key only when
    non-empty, so single-room recipes' artifacts are byte-identical to the
    pre-shadow shape. The PR 4 shadow→live measurement gate reads them.
    """
    mode_value = getattr(mode, "value", mode)
    rows = [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in report.results]
    passed = sum(1 for r in report.results if r.passed)
    total = len(report.results)
    out: dict[str, Any] = {
        "eval_id": report.eval_id,
        "tier": tier,
        "mode": mode_value,
        "passed": report.passed,
        "summary": {"total": total, "passed": passed, "failed": total - passed},
        "assertions": rows,
    }
    if shadow_traces:
        out["shadow_traces"] = shadow_traces
    return out


def suite_report(eval_dicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-recipe artifact dicts into a suite-level report.

    ``passed_all`` is the merge-gate signal Phase 2 will read: true iff every
    recipe in the suite passed.
    """
    passed = sum(1 for d in eval_dicts if d.get("passed"))
    total = len(eval_dicts)
    return {
        "evals": eval_dicts,
        "summary": {
            "evals": total,
            "passed": passed,
            "failed": total - passed,
            "passed_all": passed == total,
        },
    }


def write_report(path: str | Path, suite: dict[str, Any]) -> None:
    """Write a suite report to ``path`` as pretty, sorted JSON (stable diffs)."""
    text = json.dumps(suite, indent=2, sort_keys=True, ensure_ascii=False)
    Path(path).write_text(text + "\n", encoding="utf-8")
