"""RFC 0049 PR 4 — the cross-room seeds replay green and the verdict is real.

Two seeds pin the two postures of the ``memory.{facts,episodic}.cross_room``
knobs around the shadow→live promotion:

* ``EVAL-MEMORY-002`` (SHADOW) — the walled room-B prompt, with the widened
  recall's would-inject delta recorded as ``shadow_traces`` in the report
  artifact. Its replay IS the reproducible promotion measurement: this file
  re-runs the verdict (:mod:`evaluators.shadow_measurement`) over the traces
  it captures, with the tier bounds taken from the LIVE runtime constants so
  the pure module's defaults cannot silently drift off them.
* ``EVAL-MEMORY-003`` (LIVE) — the promoted posture: the room-B prompt
  carries the DM-taught facts. The strip leg proves the widening is
  load-bearing at the request-hash level: the same golden replayed under a
  shadow-pinned override must MISS the cassette on the asking turn, because
  the recorded request contains cross-room content a walled prompt cannot
  reproduce.

Subprocess replays for the same reason as ``test_eval_seed_replay.py``:
request hashes are sensitive to process-global runtime state, and a fresh
process is exactly how ``make eval-replay`` (and CI) consumes a golden.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from evaluators.runner import golden_path_for
from evaluators.shadow_measurement import promotion_verdict

_REPO = Path(__file__).resolve().parents[2]
_EVAL_SETS = _REPO / "evaluators" / "eval_sets"
_OFFLINE_OPTIMIZATION = _REPO / "config" / "demo" / "offline" / "optimization.yaml"
_OFFLINE_RESPONSES = _EVAL_SETS / "offline_responses.eval.yaml"
_SHADOW_ID = "EVAL-MEMORY-002"
_LIVE_ID = "EVAL-MEMORY-003"


def _recipe_path(recipe_id: str) -> Path:
    return _EVAL_SETS / f"{recipe_id}.yaml"


def _run_replay(
    recipe_id: str,
    *,
    eval_sets_dir: Path | None = None,
    report_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Replay one seed in a fresh process — the real ``make eval-replay`` path,
    offline optimization overlay pinned (the goldens were recorded under it)."""
    cmd = [
        sys.executable, "-m", "evaluators.runner",
        "--mode", "replay", "--target", recipe_id,
    ]
    if eval_sets_dir is not None:
        cmd += ["--eval-sets-dir", str(eval_sets_dir)]
    if report_path is not None:
        cmd += ["--report", str(report_path)]
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        cmd, cwd=_REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=180,
    )


# ─── the shadow seed: green replay + a real, green promotion verdict ─────────


def test_shadow_seed_replays_green_and_verdict_is_green(tmp_path: Path) -> None:
    """``EVAL-MEMORY-002`` replays green and its captured traces render a green
    promotion verdict — the committed, reproducible form of the measurement the
    shadow→live flip was gated on.

    The tier bounds come from the live runtime constants (not the pure
    module's copies), and both withhold fields are read off the trace — the
    0031-amendment trace-shape contract the PR 2/3 reviews required of this
    consumer."""
    from agents.persona_runtime.episodic_section import EPISODIC_RECALL_LIMIT
    from agents.persona_runtime.facts_section import FACTS_RECALL_LIMIT

    report_path = tmp_path / "report.json"
    result = _run_replay(_SHADOW_ID, report_path=report_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"[PASS] {_SHADOW_ID}" in result.stdout

    report = json.loads(report_path.read_text(encoding="utf-8"))
    (entry,) = report["evals"]
    traces = entry.get("shadow_traces")
    assert traces, (
        "the shadow seed must capture a non-empty shadow_traces stream — an "
        "empty one means the cross-room delta never fired (the measurement "
        "would be vacuous)"
    )

    verdict = promotion_verdict(
        traces,
        goldens_green=bool(report["summary"]["passed_all"]),
        tier_bounds={
            "episodic": EPISODIC_RECALL_LIMIT,
            "facts": FACTS_RECALL_LIMIT,
        },
    )
    assert verdict.green, verdict.to_dict()
    (facts_tier,) = [t for t in verdict.tiers if t.tier == "facts"]
    # The curated arc plants exactly two cross-room facts (one topic-seeded,
    # one person-seeded) — both must be gate-admitted at the internal acting
    # level, with zero withholds of either cause.
    assert facts_tier.candidate_count == 2, verdict.to_dict()
    assert facts_tier.withheld == 0
    assert facts_tier.unknown_label == 0


# ─── the live seed: green replay + the widening is load-bearing ──────────────


def test_live_seed_replays_green() -> None:
    result = _run_replay(_LIVE_ID)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"[PASS] {_LIVE_ID}" in result.stdout


def test_live_widening_is_load_bearing(tmp_path: Path) -> None:
    """Replaying the LIVE golden under a shadow-pinned override must MISS the
    cassette: the recorded asking-turn request carries the cross-room facts
    section, which a walled prompt cannot reproduce. A regression that
    re-walls the live recall fails the committed seed exactly this way —
    the request-hash pin, not the (mock-authored) transcript, is what makes
    EVAL-MEMORY-003 load-bearing."""
    recipe = yaml.safe_load(_recipe_path(_LIVE_ID).read_text(encoding="utf-8"))
    recipe["setup"]["memory"] = {
        "facts": {"cross_room": "shadow"},
        "episodic": {"cross_room": "shadow"},
    }
    (tmp_path / f"{_LIVE_ID}.yaml").write_text(
        yaml.safe_dump(recipe), encoding="utf-8",
    )
    shutil.copy(
        golden_path_for(_recipe_path(_LIVE_ID)),
        tmp_path / f"{_LIVE_ID}.golden.yaml",
    )

    result = _run_replay(_LIVE_ID, eval_sets_dir=tmp_path)

    assert result.returncode != 0, (
        "a shadow-pinned replay of the live golden must fail — if it passes, "
        "the live prompt never actually widened and the seed is vacuous:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    # The miss surfaces through the runtime's LLM-error wrapping, so match the
    # ReplayCassetteMissError message, not the class name.
    assert "no recorded response for request" in result.stderr, (
        result.stdout, result.stderr,
    )


# ─── both goldens re-record deterministically (portability) ──────────────────


def _run_record_offline(
    recipe_id: str, eval_sets_dir: Path
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable, "-m", "evaluators.runner",
        "--mode", "record", "--target", recipe_id,
        "--eval-sets-dir", str(eval_sets_dir),
    ]
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PERSATRIX_OFFLINE_RESPONSES": str(_OFFLINE_RESPONSES),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        cmd, cwd=_REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=180,
    )


def test_offline_record_is_deterministic(tmp_path: Path) -> None:
    """A fresh offline re-record reproduces both committed cross-room goldens
    (parsed-cassette comparison — the ``test_eval_seed_replay`` portability
    contract, extended to the multi-room driver path: the derived room
    sessions and message ids are deterministic, so record is host-independent)."""
    for recipe_id in (_SHADOW_ID, _LIVE_ID):
        recipe_dir = tmp_path / recipe_id
        recipe_dir.mkdir()
        shutil.copy(_recipe_path(recipe_id), recipe_dir / f"{recipe_id}.yaml")

        result = _run_record_offline(recipe_id, recipe_dir)
        assert result.returncode == 0, (result.stdout, result.stderr)

        fresh = yaml.safe_load(
            (recipe_dir / f"{recipe_id}.golden.yaml").read_text(encoding="utf-8"),
        )
        committed = yaml.safe_load(
            golden_path_for(_recipe_path(recipe_id)).read_text(encoding="utf-8"),
        )
        assert fresh == committed, (
            f"{recipe_id}: a fresh offline re-record must reproduce the "
            "committed golden's request hashes + payloads"
        )
