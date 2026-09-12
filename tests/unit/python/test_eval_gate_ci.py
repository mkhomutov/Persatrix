"""Pin the eval replay merge gate (RFC 0044 Phase 2, v0.3.16 PR C2).

Phase 1 shipped a runner whose report a human read; nothing in CI ran
`make eval-replay`, so a golden that stopped replaying blocked no merge
unless a seed happened to have its own integration test. These tests pin
the three parts of the gate: the CI step in the required Python job, the
Makefile passing the tier through to the runner, and a `stable` tier that
is not empty — a gate over no recipes is vacuous, not green.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_SETS = REPO_ROOT / "evaluators" / "eval_sets"


def _makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _python_job_steps() -> list[dict[str, Any]]:
    workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return list(ci["jobs"]["python"]["steps"])


def _recipes() -> list[Path]:
    return sorted(p for p in EVAL_SETS.glob("EVAL-*.yaml") if not p.name.endswith(".golden.yaml"))


def test_ci_python_job_replays_the_stable_tier() -> None:
    """The required Python job runs the same target a developer runs, tier-scoped."""
    wanted = "make eval-replay TIER=stable"
    runs = [s for s in _python_job_steps() if str(s.get("run", "")).strip() == wanted]
    assert len(runs) == 1, "the python job must run exactly `make eval-replay TIER=stable`"


def test_makefile_eval_replay_passes_the_tier_to_the_runner() -> None:
    recipe = re.search(r"^eval-replay:.*?\n((?:\t.*\n)+)", _makefile(), re.M | re.S)
    assert recipe is not None, "no eval-replay recipe"
    body = recipe.group(1)
    assert "$(if $(TIER),--tier $(TIER),)" in body, "eval-replay does not pass TIER as --tier"


def test_committed_stable_tier_is_not_empty_and_every_member_has_a_golden() -> None:
    """RFC 0044 §F: the seeds start in `stable`; a stable recipe replays a recorded golden."""
    stable = [
        p for p in _recipes()
        if yaml.safe_load(p.read_text(encoding="utf-8")).get("tier") == "stable"
    ]
    assert stable, "no committed recipe is in the stable tier — the CI gate would run nothing"
    missing = [p.name for p in stable if not p.with_name(f"{p.stem}.golden.yaml").is_file()]
    assert not missing, f"stable recipes without a recorded golden: {missing}"
