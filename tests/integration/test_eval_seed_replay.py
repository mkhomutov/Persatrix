"""RFC 0044 PR 4 — the first pre-0041 seed golden replays green.

This is the seed the harness was built for: the dementia-test recipe
(:data:`EVAL-MEMORY-001`, source [MT-MEMORY-005]) with a committed
``.golden.yaml`` sidecar recorded offline against the mock provider. It is the
first golden that replays against the **pre-0041 surface** — its assertions are
restricted to the subset that does not need typed events (``final_transcript`` /
``terminal_state``), so it lands ahead of RFC 0041 ([RFC 0044 §Phase 1]). The
event-asserting seeds (``EVAL-ERROR-001`` / ``002``) and the remaining recipes
follow in a 4b slice once RFC 0041 emits their events.

Determinism is real, not incidental: the driver forces an isolated ``:memory:``
DB and a ``FrozenClock``, so replaying the committed golden on a *fresh* clone —
with no ambient memory rows and no API key — reproduces the recorded requests
byte-for-byte. A regression in the memory-recall → prompt-assembly path shifts a
request, misses the cassette, and fails the replay — exactly the automated
regression bar RFC 0044 §M-1 promises. The eval harness's *tiered* merge gate
(`passed_all` blocking the `stable` tier) is the separate Phase-2 step, still
deferred; but this pytest lives in CI's ``tests/integration/`` job, so a replay
regression fails CI today.

**Why a subprocess.** The replay legs shell out to ``python -m evaluators.runner``
— the exact ``make eval-replay`` path CI runs — rather than driving the runner
in-process. The golden is recorded in a fresh process, and a replay's request
hashes are sensitive to process-global runtime state (a prior in-process persona
build warms caches that shift the assembled prompt). Running in a fresh process
is hermetic: it matches how the golden is produced and consumed, and it is immune
to whatever else the test session touched. The non-replay legs (recipe shape,
missing-golden) stay in-process — they build no prompt, so they are order-immune.

[MT-MEMORY-005]: ../../docs/manual-tests/MT-MEMORY-005-dementia-test.md
[RFC 0044 §Phase 1]: ../../docs/rfcs/0044-eval-set-golden-traces.md#phased-implementation-plan
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from evaluators.eval_set import load_eval_set
from evaluators.runner import EvalMode, discover_recipes, golden_path_for, run_suite

_REPO = Path(__file__).resolve().parents[2]
_EVAL_SETS = _REPO / "evaluators" / "eval_sets"
_OFFLINE_OPTIMIZATION = _REPO / "config" / "demo" / "offline" / "optimization.yaml"
_RECIPE_ID = "EVAL-MEMORY-001"


def _recipe_path() -> Path:
    return _EVAL_SETS / f"{_RECIPE_ID}.yaml"


def _run_replay(eval_sets_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Replay the seed in a fresh process — the real ``make eval-replay`` path.

    Pins the offline optimization overlay the golden was recorded under: the
    action loop hashes the raw ``quality`` alias (env-independent), but the RFC
    0020 close-summary / RFC 0051 critic paths hash the *resolved physical*
    model, so replay must resolve the same aliases the record did.
    """
    cmd = [sys.executable, "-m", "evaluators.runner", "--mode", "replay", "--target", _RECIPE_ID]
    if eval_sets_dir is not None:
        cmd += ["--eval-sets-dir", str(eval_sets_dir)]
    # Force UTF-8 in the child so the runner's non-ASCII summary glyphs (the ✗ on
    # a failed-assertion line) never raise UnicodeEncodeError and truncate stdout
    # under a C/ASCII CI locale — otherwise a working failure could read as a
    # spurious red. Decode our capture as UTF-8 for the same reason.
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        cmd, cwd=_REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180
    )


# ─── the committed seed replays green ────────────────────────────────────────


def test_seed_recipe_replays_green() -> None:
    """The committed ``EVAL-MEMORY-001`` recipe + golden replays all-pass through
    the real persona runtime — no API key, no network, deterministic."""
    assert _recipe_path().is_file(), (
        f"{_RECIPE_ID} recipe must be committed under evaluators/eval_sets/"
    )
    assert golden_path_for(_recipe_path()).is_file(), (
        f"{_RECIPE_ID}.golden.yaml must be committed beside its recipe (replay needs it)"
    )

    result = _run_replay()

    assert result.returncode == 0, (
        f"{_RECIPE_ID} did not replay green:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert "[PASS] EVAL-MEMORY-001" in result.stdout
    assert "1/1 recipes passed" in result.stdout


def test_seed_recipe_is_pre_rfc0041_subset() -> None:
    """The seed asserts only over ``final_transcript`` / ``terminal_state`` — no
    typed-event assertions, which need RFC 0041 (the Phase 1 runner reports an
    empty event stream). This guards the recipe from acquiring an event
    assertion that would silently never run until 4b."""
    es = load_eval_set(_recipe_path())
    assert es.assertions.final_transcript, "the dementia seed asserts on the transcript"
    assert es.assertions.terminal_state, "the dementia seed asserts on terminal trust state"
    assert not es.assertions.event_count, "event_count needs RFC 0041 — defer to 4b"
    assert es.assertions.event_sequence is None, "event_sequence needs RFC 0041 — defer to 4b"
    assert all(not t.events for i in es.interactions for t in i.turns), (
        "per-turn events need RFC 0041 — defer to 4b"
    )


# ─── the golden is load-bearing (no vacuous pass) ────────────────────────────


def test_seed_recipe_golden_is_load_bearing(tmp_path: Path) -> None:
    """The assertions are load-bearing: a recipe whose expectation contradicts the
    recorded replies fails — proof the eval is not a vacuous pass (RFC 0044 §D).

    Mutating an *assertion* (not the golden) keeps every request hash identical, so
    the replay still HITS the committed cassette on every turn — the failure is a
    genuine assertion verdict on the real recorded transcript, never a swallowed
    cassette miss. Here the transcript is asserted to NOT reference "Mira", which
    it demonstrably does (i1 + i4), so the run must go red."""
    recipe = yaml.safe_load(_recipe_path().read_text(encoding="utf-8"))
    recipe["assertions"]["final_transcript"]["must_not_reference"] = ["Mira"]
    (tmp_path / f"{_RECIPE_ID}.yaml").write_text(yaml.safe_dump(recipe), encoding="utf-8")
    shutil.copy(golden_path_for(_recipe_path()), tmp_path / f"{_RECIPE_ID}.golden.yaml")

    result = _run_replay(eval_sets_dir=tmp_path)

    assert result.returncode != 0, "a contradicted assertion must fail — the eval is not vacuous"
    assert "[FAIL] EVAL-MEMORY-001" in result.stdout, result.stdout
    assert "must_not_reference" in result.stdout, result.stdout


async def test_missing_golden_fails_loud(tmp_path: Path) -> None:
    """A recipe whose golden was never recorded must fail loudly, never silently
    pass (RFC 0044 §D) — replay builds the provider from the sidecar and raises.

    In-process: this leg raises in ``build_provider`` before any prompt is built,
    so it is order-immune and needs no fresh process."""
    shutil.copy(_recipe_path(), tmp_path / f"{_RECIPE_ID}.yaml")  # recipe, no golden
    recipes = discover_recipes(tmp_path, target=_RECIPE_ID)
    agents_cfg = str(_REPO / "config" / "agents.yaml")
    # match= pins this to the golden-missing raise (build_provider), not an
    # unrelated FileNotFoundError (e.g. a resolver reading a missing config).
    with pytest.raises(FileNotFoundError, match="no golden"):
        await run_suite(recipes, mode=EvalMode.REPLAY, config_path=agents_cfg)
