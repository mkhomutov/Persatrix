"""RFC 0044 — the working-memory seed golden replays green, and its golden is
load-bearing *on working memory*.

``EVAL-WORKING-001`` is the second pre-0041 seed (source [RFC 0034]): a
single-interaction recipe where the persona must reference its own prior
clarifying question. Unlike the dementia seed (:mod:`test_eval_seed_replay`,
cross-interaction long-term recall), this one exercises the RFC 0034 conversation
window (working memory) — a distinct runtime path. The recipe declares
``setup.channel``, so the eval driver wires an in-process history fetcher and the
persona's second-turn prompt carries the reconstructed transcript, including its
own first reply.

That makes the golden load-bearing on working memory, not merely on prompt-assembly
stability: :func:`test_working_memory_is_load_bearing` replays the committed golden
against a channel-stripped copy of the recipe (working memory off) and shows the
run goes red — turn 2's request no longer carries the window, so its hash misses
the cassette. A regression that dropped the persona's own prior turn from the
window would fail this seed exactly the same way.

**Why a subprocess.** Like the dementia seed, the replay legs shell out to
``python -m evaluators.runner`` (the real ``make eval-replay`` path). Request
hashes are sensitive to process-global runtime state — and the conversation window
adds one more: the in-process ``_WINDOW_CACHE`` LRU
(``agents/persona_runtime/_conversation_window_cache.py``) warmed by a prior
in-process persona build could shift the assembled window. A fresh process is
hermetic — it matches how the golden is produced and consumed.

[RFC 0034]: ../../docs/rfcs/0034-persona-conversational-working-memory.md
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from evaluators.eval_set import load_eval_set
from evaluators.runner import golden_path_for

_REPO = Path(__file__).resolve().parents[2]
_EVAL_SETS = _REPO / "evaluators" / "eval_sets"
_OFFLINE_OPTIMIZATION = _REPO / "config" / "demo" / "offline" / "optimization.yaml"
_RECIPE_ID = "EVAL-WORKING-001"


def _recipe_path() -> Path:
    return _EVAL_SETS / f"{_RECIPE_ID}.yaml"


def _run_replay(eval_sets_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Replay the seed in a fresh process — the real ``make eval-replay`` path,
    with the offline optimization overlay the golden was recorded under pinned."""
    cmd = [sys.executable, "-m", "evaluators.runner", "--mode", "replay", "--target", _RECIPE_ID]
    if eval_sets_dir is not None:
        cmd += ["--eval-sets-dir", str(eval_sets_dir)]
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        cmd, cwd=_REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180
    )


# ─── the committed seed replays green ────────────────────────────────────────


def test_working_seed_replays_green() -> None:
    """The committed ``EVAL-WORKING-001`` recipe + golden replays all-pass through
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
    assert "[PASS] EVAL-WORKING-001" in result.stdout
    assert "1/1 recipes passed" in result.stdout


def test_working_seed_is_pre_rfc0041_subset() -> None:
    """The seed asserts only over ``final_transcript`` / ``terminal_state`` — no
    typed-event assertions, which need RFC 0041. Guards the recipe from acquiring
    an event assertion that would silently never run until 4b."""
    es = load_eval_set(_recipe_path())
    assert es.setup.channel, "the working-memory seed must declare a channel (engages RFC 0034)"
    assert es.assertions.final_transcript, "the working-memory seed asserts on the transcript"
    assert es.assertions.terminal_state, "the working-memory seed asserts on terminal trust state"
    assert not es.assertions.event_count, "event_count needs RFC 0041 — defer to 4b"
    assert es.assertions.event_sequence is None, "event_sequence needs RFC 0041 — defer to 4b"
    assert all(not t.events for i in es.interactions for t in i.turns), (
        "per-turn events need RFC 0041 — defer to 4b"
    )


# ─── the golden is load-bearing on the assertion … ───────────────────────────


def test_working_seed_golden_is_load_bearing(tmp_path: Path) -> None:
    """A recipe whose expectation contradicts the recorded replies fails — proof
    the eval is not a vacuous pass (RFC 0044 §D).

    Mutating an *assertion* (not the golden) keeps every request hash identical, so
    replay still HITS the committed cassette on every turn — the failure is a
    genuine assertion verdict on the real recorded transcript. Here the transcript
    is asserted to NOT reference "staging", which it demonstrably does (both
    replies), so the run must go red."""
    recipe = yaml.safe_load(_recipe_path().read_text(encoding="utf-8"))
    recipe["assertions"]["final_transcript"]["must_not_reference"] = ["staging"]
    (tmp_path / f"{_RECIPE_ID}.yaml").write_text(yaml.safe_dump(recipe), encoding="utf-8")
    shutil.copy(golden_path_for(_recipe_path()), tmp_path / f"{_RECIPE_ID}.golden.yaml")

    result = _run_replay(eval_sets_dir=tmp_path)

    assert result.returncode != 0, "a contradicted assertion must fail — the eval is not vacuous"
    assert "[FAIL] EVAL-WORKING-001" in result.stdout, result.stdout
    assert "must_not_reference" in result.stdout, result.stdout


# ─── … and load-bearing on working memory specifically ───────────────────────


def test_working_memory_is_load_bearing(tmp_path: Path) -> None:
    """The golden pins the RFC 0034 conversation window, not just assembly
    stability: replay the committed golden against a channel-stripped copy of the
    recipe (working memory OFF) and the run goes red.

    With no channel the driver drives the current-event-only path, so turn 2's
    request loses the reconstructed window (turn 1's user message + the persona's
    own reply). Its hash no longer matches the cassette recorded *with* the window,
    the persona gets no recorded reply, and ``must_reference`` fails. This is the
    exact failure a regression that dropped the persona's prior turn would produce
    — so the seed genuinely guards the working-memory path."""
    stripped = "\n".join(
        line for line in _recipe_path().read_text(encoding="utf-8").splitlines()
        if "channel:" not in line
    )
    (tmp_path / f"{_RECIPE_ID}.yaml").write_text(stripped, encoding="utf-8")
    shutil.copy(golden_path_for(_recipe_path()), tmp_path / f"{_RECIPE_ID}.golden.yaml")

    result = _run_replay(eval_sets_dir=tmp_path)

    assert result.returncode != 0, (
        "dropping the channel must fail the replay — else the golden does not "
        f"actually pin working memory:\n{result.stdout}"
    )
    assert "[FAIL] EVAL-WORKING-001" in result.stdout, result.stdout
