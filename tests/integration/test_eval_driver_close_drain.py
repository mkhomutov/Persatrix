"""Adversarial repro of the 2026-07-28 ``main`` CI failure — a delayed
Phase-2 close finalisation must not shift a golden replay.

``EVAL-MEMORY-003``'s ask-turn prompt depends on the facts the Phase-2
close finalisation writes when the 11 m gap rotates the DM interaction.
That finalisation is a fire-and-forget task, so pre-drain the recorded
"facts landed first" interleaving held only by scheduling luck: on a
contended CI runner the task lost the race ~half the time and the
ask-turn request missed the cassette (``no recorded response for
request 1db474a5…`` / ``turn[1] missing 'Friday'``).

This test makes the race deterministic-adversarial: the finalisation is
wrapped in a 50 ms delay, so *without* the driver's per-turn
``drain_pending_summaries()`` await it always loses — reproducing the CI
signature on every machine — and *with* the drain the replay is green no
matter how slow finalisation is. Hermetic-subprocess form, like
``test_cross_room_seed_replay.py`` (the goldens were recorded under the
offline optimization overlay; in-process pytest state would shift the
request hashes).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_OFFLINE_OPTIMIZATION = _REPO / "config" / "demo" / "offline" / "optimization.yaml"

# Patch the ``close_path`` module global (bound at call time, line 117)
# before handing control to the real runner ``__main__``.
_BOOTSTRAP = textwrap.dedent(
    """
    import asyncio
    import runpy
    import sys

    import agents.persona_runtime.close_path as close_path

    _orig = close_path.finalize_closed_interaction

    async def _delayed_finalize(*args, **kwargs):
        await asyncio.sleep(0.05)
        return await _orig(*args, **kwargs)

    close_path.finalize_closed_interaction = _delayed_finalize
    sys.argv = [
        "evaluators.runner", "--mode", "replay", "--target", "EVAL-MEMORY-003",
    ]
    runpy.run_module("evaluators.runner", run_name="__main__")
    """
)


def test_delayed_finalize_cannot_shift_the_golden_replay() -> None:
    env = {
        **os.environ,
        "PERSATRIX_OPTIMIZATION_CONFIG": str(_OFFLINE_OPTIMIZATION),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP],
        cwd=_REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=180,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[PASS] EVAL-MEMORY-003" in result.stdout
