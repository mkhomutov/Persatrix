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
_OFFLINE_RESPONSES = _EVAL_SETS / "offline_responses.eval.yaml"
_RECIPE_ID = "EVAL-MEMORY-001"


def _recipe_path() -> Path:
    return _EVAL_SETS / f"{_RECIPE_ID}.yaml"


def _run_replay(
    eval_sets_dir: Path | None = None, *, io_encoding: str = "utf-8"
) -> subprocess.CompletedProcess[str]:
    """Replay the seed in a fresh process — the real ``make eval-replay`` path.

    Pins the offline optimization overlay the golden was recorded under: the
    action loop hashes the raw ``quality`` alias (env-independent), but the RFC
    0020 close-summary / RFC 0051 critic paths hash the *resolved physical*
    model, so replay must resolve the same aliases the record did.

    ``io_encoding`` sets the child's ``PYTHONIOENCODING`` — UTF-8 by default; the
    non-UTF-8-stdout guard overrides it to ``ascii`` to prove the runner's stdout
    reconfigure keeps a failing summary (the ✗ glyph) from crashing.
    """
    cmd = [sys.executable, "-m", "evaluators.runner", "--mode", "replay", "--target", _RECIPE_ID]
    if eval_sets_dir is not None:
        cmd += ["--eval-sets-dir", str(eval_sets_dir)]
    # We always decode the child's stdout as UTF-8 (below); the runner self-defends by
    # reconfiguring its stdout to UTF-8 with errors="replace" (evaluators/runner.py
    # main()), so the non-ASCII summary glyph (the ✗ on a failed-assertion line) never
    # raises UnicodeEncodeError even when io_encoding is non-UTF-8.
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PYTHONIOENCODING": io_encoding,
    }
    return subprocess.run(
        cmd, cwd=_REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180
    )


def _run_record_offline(eval_sets_dir: Path) -> subprocess.CompletedProcess[str]:
    """Re-record the seed offline in a fresh process — the real
    ``make eval-record-offline`` path (mock provider, $0, no API key).

    Sets the offline optimization overlay + the curated responses fixture exactly
    as the make target does, so the golden it writes into ``eval_sets_dir`` is
    produced the same way the committed one was.
    """
    cmd = [
        sys.executable, "-m", "evaluators.runner",
        "--mode", "record", "--target", _RECIPE_ID,
        "--eval-sets-dir", str(eval_sets_dir),
    ]
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PERSATRIX_OFFLINE_RESPONSES": str(_OFFLINE_RESPONSES),
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


def test_replay_summary_survives_non_utf8_stdout(tmp_path: Path) -> None:
    """A failing replay under a non-UTF-8 stdout still prints its whole summary and
    exits 1 — it never crashes on the ``✗`` glyph (``evaluators/runner.py`` main()
    reconfigures stdout to UTF-8). ``PYTHONIOENCODING=ascii`` is exactly the encoding
    a bare ``print('✗')`` dies on (Windows cp1252 hits the same wall), and Phase 2
    gates CI on this ``make eval-replay`` path — a crash there would read as a
    spurious red with the verdict truncated.

    Forces the red path with the load-bearing mutation (a contradicted assertion),
    which keeps every request hash identical so replay still hits the committed
    cassette — the ``✗`` is emitted on a genuine assertion verdict, not a miss."""
    recipe = yaml.safe_load(_recipe_path().read_text(encoding="utf-8"))
    recipe["assertions"]["final_transcript"]["must_not_reference"] = ["Mira"]
    (tmp_path / f"{_RECIPE_ID}.yaml").write_text(yaml.safe_dump(recipe), encoding="utf-8")
    shutil.copy(golden_path_for(_recipe_path()), tmp_path / f"{_RECIPE_ID}.golden.yaml")

    result = _run_replay(eval_sets_dir=tmp_path, io_encoding="ascii")

    assert "UnicodeEncodeError" not in result.stderr, result.stderr
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "[FAIL] EVAL-MEMORY-001" in result.stdout, result.stdout
    assert "must_not_reference" in result.stdout, result.stdout  # the ✗ line printed
    assert "eval suite:" in result.stdout, result.stdout  # _print_summary ran to completion


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


# ─── the offline record is byte-deterministic (portability guard) ────────────


def test_offline_record_is_deterministic(tmp_path: Path) -> None:
    """A fresh ``make eval-record-offline`` reproduces the committed golden's
    content — the determinism/portability bar RFC 0044 §D leans on.

    Replay only proves *that committed file* replays green; it does not prove a
    re-record reproduces it (a differently-recorded-but-still-green golden would
    pass replay yet break the portability contract). This closes that gap:
    canonicalized requests carry no platform-dependent bytes and the mock's token
    counts are length-derived, so the offline record is host-independent — a drift
    in the offline fixtures or the memory-recall → prompt-assembly path is the only
    thing that should move it.

    Compares the *parsed* cassette (request-hash keys + response payloads), not raw
    bytes: byte-identity would additionally couple this test to the PyYAML emitter's
    version-specific scalar line-folding (`pyyaml` is pinned only `>=6.0,<7`), a
    toolchain-skew flake unrelated to eval determinism. The parsed mapping IS the
    portability contract — identical request hashes across the record host and CI.
    (Byte-identity does hold today; it is just not a robust CI invariant to assert.)

    Records into a tmp dir (never the committed sidecar), so the check is
    side-effect-free."""
    # Copy the recipe only (no golden) — record mode writes the golden sidecar itself.
    shutil.copy(_recipe_path(), tmp_path / f"{_RECIPE_ID}.yaml")

    result = _run_record_offline(tmp_path)

    assert result.returncode == 0, (
        f"offline record failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    fresh = tmp_path / f"{_RECIPE_ID}.golden.yaml"
    assert fresh.is_file(), "record mode must write the golden sidecar"
    committed = golden_path_for(_recipe_path())
    fresh_cassette = yaml.safe_load(fresh.read_text(encoding="utf-8"))
    committed_cassette = yaml.safe_load(committed.read_text(encoding="utf-8"))
    assert fresh_cassette == committed_cassette, (
        "a fresh offline re-record must reproduce the committed golden's request "
        "hashes + payloads (RFC 0044 §D) — the offline fixtures or the prompt-assembly "
        "path drifted, or the golden is not portable across the record host and CI"
    )
