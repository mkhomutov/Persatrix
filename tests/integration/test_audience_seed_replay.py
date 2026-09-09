"""ISSUE-0132 (v0.3.16 PR A2) — the audience seed replays and the delta is real.

``EVAL-MEMORY-005`` is the offline sample the audience shadow verdict is
measured over: Alice teaches in her DM, the interaction closes, and she
asks twice — once in a room Bob is in (*disjoint*), once in a room whose
every member was in the DM (*admit*). This file is the committed,
reproducible form of that measurement — the `EVAL-MEMORY-002` precedent,
which does the same job for the RFC 0049 promotion — plus the two
properties a reader of the verdict has to be able to trust:

* the sample is **not vacuous** — a disjoint audience actually occurred,
  which is exactly what the fourth criterion asserts and what a
  same-room-only run would fail;
* the check is **shadow** — the entry was still injected, which is the
  byte-identity claim this release ships on.

Subprocess replays for the same reason as the sibling seed-replay
suites: request hashes are sensitive to process-global runtime state,
and a fresh process is how ``make eval-replay`` (and CI) consumes a
golden.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from evaluators.shadow_measurement import (
    AUDIENCE_TURN_BOUND,
    promotion_verdict,
    summarize_audience,
)

_REPO = Path(__file__).resolve().parents[2]
_EVAL_SETS = _REPO / "evaluators" / "eval_sets"
_OFFLINE_OPTIMIZATION = _REPO / "config" / "demo" / "offline" / "optimization.yaml"
_ID = "EVAL-MEMORY-005"

_DM = "dm:alice:ember-owl"
_WITH_BOB = "group:standup"
_WITHOUT_BOB = "group:pair"


def _run_replay(report_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, "-m", "evaluators.runner",
            "--mode", "replay", "--target", _ID, "--report", str(report_path),
        ],
        cwd=_REPO,
        env={
            **os.environ,
            "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )


def _traces(tmp_path: Path) -> list[dict]:
    report_path = tmp_path / "report.json"
    result = _run_replay(report_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"[PASS] {_ID}" in result.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    (entry,) = report["evals"]
    return [
        t for t in (entry.get("shadow_traces") or [])
        if t.get("tier") == "audience"
    ]


def test_the_seed_contributes_both_conclusive_verdicts(tmp_path: Path) -> None:
    """What this ONE recipe owes the sample: a non-empty trace stream
    carrying both conclusive verdicts.

    Not ``verdict.green``: ``audience_delta_measured`` also asks that the
    delta span at least :data:`AUDIENCE_MIN_JUDGED_TIERS` of the three
    judged tiers, and tier coverage is a property of the whole replay
    report — what ``make eval-verdict`` renders — not of a single recipe.
    Asserting it here would pin this seed's tier mix, which is the
    opposite of what the clause is for.
    """
    traces = _traces(tmp_path)
    assert traces, (
        "the audience seed must capture audience traces — an empty stream "
        "means the check never ran and the measurement is vacuous"
    )
    summary = summarize_audience(traces)
    assert summary.verdicts["withhold-disjoint"] == 1, summary
    assert summary.verdicts["admit"] == 1, summary
    assert summary.max_judged_per_turn <= AUDIENCE_TURN_BOUND, summary
    # The three criteria that are this recipe's to satisfy still hold.
    verdict = promotion_verdict(traces, goldens_green=True)
    assert verdict.green, verdict.to_dict()


def test_the_tier_the_seed_measures_is_named_not_assumed(tmp_path: Path) -> None:
    """``by_tier`` exists so a reader of the verdict can see WHICH of the
    three judged tiers a sample exercised.  This seed exercises ``facts``
    and says so — the visibility that keeps a one-tier delta from reading
    as a whole-check measurement."""
    summary = summarize_audience(_traces(tmp_path))
    assert set(summary.by_tier) == {"facts"}, summary.by_tier
    assert summary.by_tier["facts"]["withhold-disjoint"] == 1


def test_both_halves_of_the_regression_are_exercised(tmp_path: Path) -> None:
    """The room with Bob withholds-disjoint; the room without him admits.
    One verdict without the other is a sample that measured nothing —
    the vacuity failure scope lock 1 names."""
    by_room = {
        t["acting_channel_id"]: t for t in _traces(tmp_path)
    }
    assert set(by_room) == {_WITH_BOB, _WITHOUT_BOB}, sorted(by_room)

    disjoint = by_room[_WITH_BOB]["verdicts"]
    assert disjoint["withhold-disjoint"] == 1, disjoint
    assert disjoint["admit"] == 0, disjoint

    admit = by_room[_WITHOUT_BOB]["verdicts"]
    assert admit["admit"] == 1, admit
    assert admit["withhold-disjoint"] == 0, admit

    # Both judged the SAME DM-taught entry — otherwise the two rooms are
    # measuring two different facts and the comparison means nothing.
    ids = {
        c["entry_id"]
        for t in by_room.values() for c in t["candidates"]
    }
    assert len(ids) == 1, ids
    for trace in by_room.values():
        for candidate in trace["candidates"]:
            assert candidate["source_channel_id"] == _DM, candidate
            assert candidate["protection_level"] == "internal", candidate


def test_shadow_withheld_nothing(tmp_path: Path) -> None:
    """The byte-identity claim, from the trace side: a *disjoint* verdict
    was recorded and the entry was injected anyway."""
    traces = _traces(tmp_path)
    assert all(t["mode"] == "shadow" for t in traces), traces
    assert all(t["withheld"] == 0 for t in traces), traces


def test_no_unknown_verdicts_and_the_fetch_bound_holds(tmp_path: Path) -> None:
    """Offline, every roster resolves: the in-process seam cannot fail and
    the seed's entry always carries provenance, so an unknown of either
    cause here is a defect, not a posture. It is also why the
    fetch-failed count is a LIVE criterion and not this one.

    The fetch bound is the scope-lock-2 cost: one round trip per distinct
    source room, and the acting room — already resolved by the A1 rail —
    is FREE, so ``fetches`` is a floor of ``source_rooms`` and never an
    equality.  Asserting equality would pass only while no candidate came
    from the acting room — i.e. only while the pre-seed this bound exists
    to credit was never exercised.  The same-room case is pinned
    directly in ``tests/unit/python/test_audience_scope.py``.
    """
    summary = summarize_audience(_traces(tmp_path))
    assert summary.unknown_share == 0.0, summary
    assert summary.verdicts.get("withhold-unknown-fetch-failed", 0) == 0
    assert summary.verdicts.get("withhold-unknown-no-provenance", 0) == 0
    assert summary.fetches <= summary.source_rooms, summary
    assert summary.withhold_share == 0.5, summary
