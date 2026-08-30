"""RFC 0037 PR 8 — the confidentiality seed replays green and pins the gate.

``EVAL-MEMORY-004`` is the §D-gate golden: a fact taught acting ``restricted``
is a cross-room recall CANDIDATE on every later Zephyr-naming turn (the
RFC 0049 live widening, pinned by ``setup.memory``), so the golden pins the
GATE — the ``internal`` ask's recorded request has the candidate withheld, the
``restricted`` re-ask's has it admitted verbatim. A gate regression in either
direction (leak or over-withhold) shifts one of those requests and misses the
cassette.

The strip leg proves the per-interaction ``classification:`` extension is
load-bearing: the same golden replayed with the keys removed collapses every
turn to the driver's ``internal`` default — the taught fact then stamps
``internal``, the standup ask ADMITS it, and the replay must miss. If that
replay passes, the declared levels never reached the wire stamp and the seed
is vacuous.

Subprocess replays for the same reason as ``test_eval_seed_replay.py``:
request hashes are sensitive to process-global runtime state, and a fresh
process is exactly how ``make eval-replay`` (and CI) consumes a golden.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from evaluators.runner import golden_path_for

_REPO = Path(__file__).resolve().parents[2]
_EVAL_SETS = _REPO / "evaluators" / "eval_sets"
_OFFLINE_OPTIMIZATION = _REPO / "config" / "demo" / "offline" / "optimization.yaml"
_OFFLINE_RESPONSES = _EVAL_SETS / "offline_responses.eval.yaml"
_GATE_ID = "EVAL-MEMORY-004"


def _recipe_path(recipe_id: str) -> Path:
    return _EVAL_SETS / f"{recipe_id}.yaml"


def _run_runner(
    mode: str,
    recipe_id: str,
    *,
    eval_sets_dir: Path | None = None,
    record_fixtures: bool = False,
) -> subprocess.CompletedProcess[str]:
    """One runner invocation in a fresh process — the real ``make eval-replay``
    / ``eval-record-offline`` path, offline optimization overlay pinned (the
    golden was recorded under it)."""
    cmd = [
        sys.executable, "-m", "evaluators.runner",
        "--mode", mode, "--target", recipe_id,
    ]
    if eval_sets_dir is not None:
        cmd += ["--eval-sets-dir", str(eval_sets_dir)]
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PYTHONIOENCODING": "utf-8",
    }
    if record_fixtures:
        env["PERSATRIX_OFFLINE_RESPONSES"] = str(_OFFLINE_RESPONSES)
    return subprocess.run(
        cmd, cwd=_REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=180,
    )


# ─── the gate seed replays green ─────────────────────────────────────────────


def test_gate_seed_replays_green() -> None:
    result = _run_runner("replay", _GATE_ID)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"[PASS] {_GATE_ID}" in result.stdout


# ─── the classification stamp is load-bearing ────────────────────────────────


def test_classification_stamp_is_load_bearing(tmp_path: Path) -> None:
    """Replaying the committed golden with every ``classification:`` key
    stripped must MISS the cassette: all turns collapse to the ``internal``
    default, the taught fact stamps ``internal``, and the standup ask's
    prompt gains the fact the recorded request was gated NOT to contain
    (the restricted close also loses its §E projection request). This is the
    leak direction made observable at the request-hash level."""
    recipe = yaml.safe_load(_recipe_path(_GATE_ID).read_text(encoding="utf-8"))
    for interaction in recipe["interactions"]:
        interaction.pop("classification", None)
    (tmp_path / f"{_GATE_ID}.yaml").write_text(
        yaml.safe_dump(recipe), encoding="utf-8",
    )
    shutil.copy(
        golden_path_for(_recipe_path(_GATE_ID)),
        tmp_path / f"{_GATE_ID}.golden.yaml",
    )

    result = _run_runner("replay", _GATE_ID, eval_sets_dir=tmp_path)

    assert result.returncode != 0, (
        "a classification-stripped replay of the gate golden must fail — if "
        "it passes, the declared levels never reached the wire stamp and the "
        f"seed is vacuous:\n{result.stdout}\n{result.stderr}"
    )
    # The miss surfaces through the runtime's LLM-error wrapping, so match the
    # ReplayCassetteMissError message, not the class name.
    assert "no recorded response for request" in result.stderr, (
        result.stdout, result.stderr,
    )


# ─── the golden re-records deterministically (portability) ───────────────────


def test_offline_record_is_deterministic(tmp_path: Path) -> None:
    """A fresh offline re-record reproduces the committed gate golden
    (parsed-cassette comparison — the ``test_eval_seed_replay`` portability
    contract, extended over the classification extension: the declared levels
    are recipe data, so record is host-independent)."""
    recipe_dir = tmp_path / _GATE_ID
    recipe_dir.mkdir()
    shutil.copy(_recipe_path(_GATE_ID), recipe_dir / f"{_GATE_ID}.yaml")

    result = _run_runner(
        "record", _GATE_ID, eval_sets_dir=recipe_dir, record_fixtures=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    fresh = yaml.safe_load(
        (recipe_dir / f"{_GATE_ID}.golden.yaml").read_text(encoding="utf-8"),
    )
    committed = yaml.safe_load(
        golden_path_for(_recipe_path(_GATE_ID)).read_text(encoding="utf-8"),
    )
    assert fresh == committed, (
        "a fresh offline re-record must reproduce the committed golden's "
        "request hashes + payloads"
    )
