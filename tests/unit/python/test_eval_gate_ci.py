"""Pin the eval replay merge gate (RFC 0044 Phase 2, v0.3.16 PR C2).

Phase 1 shipped a runner whose report a human read; nothing in CI ran
`make eval-replay`, so a golden that stopped replaying blocked no merge
unless a seed happened to have its own integration test. These tests pin
the parts of the gate: the CI step in the required Python job (placed before
the unit suite, so a golden regression is the first red), the Makefile passing
the tier through to the runner, a `stable` tier that is not empty, and the
converse — every recorded golden gates unless its demotion is on record here.

Membership is computed with the runner's own helpers (`discover_recipes`,
`load_recipes`, `golden_path_for`), never a private copy of their rules, so
the test cannot stay green while the gate runs a different set.
"""

from __future__ import annotations

from _test_infra import REPO_ROOT, ci_job_steps, makefile_recipe_body

from evaluators.runner import discover_recipes, golden_path_for, load_recipes

EVAL_SETS = REPO_ROOT / "evaluators" / "eval_sets"

#: Recipes that keep a recorded golden but are deliberately out of the gate.
#: Demotion is `tier: experimental` in the recipe PLUS a row here naming the
#: reason, so leaving the gate is a visible, reviewed diff — a deleted `tier`
#: line alone fails `test_every_recorded_golden_gates_unless_demoted_on_record`.
DEMOTED: dict[str, str] = {}


def test_ci_python_job_replays_the_stable_tier_before_the_unit_suite() -> None:
    """The required Python job runs the same target a developer runs, tier-scoped,
    and ahead of the ~5-minute unit tree so a golden regression is the first red."""
    steps = ci_job_steps("python")
    runs = [str(s.get("run", "")).strip() for s in steps]
    wanted = "make eval-replay TIER=stable"
    assert runs.count(wanted) == 1, "the python job must run exactly `make eval-replay TIER=stable`"
    unit = [i for i, r in enumerate(runs) if r.startswith("python -m pytest tests/unit/python/")]
    assert unit, "no unit-test step to order against"
    assert runs.index(wanted) < unit[0], "the replay step must run before the unit suite"


def test_makefile_eval_replay_passes_the_tier_to_the_runner() -> None:
    body = makefile_recipe_body("eval-replay")
    assert "$(if $(TIER),--tier $(TIER),)" in body, "eval-replay does not pass TIER as --tier"


def test_makefile_recipe_body_stops_at_the_recipe() -> None:
    """The reader must not run past the target (the `re.S` bug the C1/C2 pins shared)."""
    body = makefile_recipe_body("eval-replay")
    lines = body.splitlines()
    assert all(line.startswith("\t") for line in lines), body
    assert len(lines) <= 4, f"eval-replay body is {len(lines)} lines — reader ran past it"


def test_committed_stable_tier_is_not_empty_and_every_member_has_a_golden() -> None:
    """RFC 0044 §F: the seeds start in `stable`; a stable recipe replays a recorded golden."""
    stable, failed = load_recipes(discover_recipes(EVAL_SETS), tier="stable")
    assert not failed, f"malformed recipes in the stable tier: {failed}"
    assert stable, "no committed recipe is in the stable tier — the CI gate would run nothing"
    missing = [r.path.name for r in stable if not golden_path_for(r.path).is_file()]
    assert not missing, f"stable recipes without a recorded golden: {missing}"


def test_every_recorded_golden_gates_unless_demoted_on_record() -> None:
    """The converse: a recipe with a golden is `stable`, or its demotion is in DEMOTED.

    Without this, deleting the one line `tier: stable` from a seed silently narrows
    the gate — the tier defaults to `experimental` and nothing else notices.
    """
    loaded, failed = load_recipes(discover_recipes(EVAL_SETS))
    assert not failed, f"malformed committed recipes: {failed}"
    ungated = [
        r.eval_set.id
        for r in loaded
        if golden_path_for(r.path).is_file()
        and r.eval_set.tier != "stable"
        and r.eval_set.id not in DEMOTED
    ]
    assert not ungated, f"recorded goldens outside the gate with no demotion on record: {ungated}"
    stale = sorted(set(DEMOTED) - {r.eval_set.id for r in loaded})
    assert not stale, f"DEMOTED names recipes that no longer exist: {stale}"
