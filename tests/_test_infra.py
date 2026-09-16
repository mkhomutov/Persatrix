"""Test infrastructure helpers shared by all pytest conftests.

Provides ``daemonize_aiosqlite_workers`` — a one-shot monkeypatch that
marks ``aiosqlite``'s per-connection worker thread as ``daemon=True`` —
and ``isolate_optimization_config``, the autouse fixture that starts
every test from the shipped ``config/optimization.yaml`` (see its
docstring).

Why
---
``aiosqlite.core.Connection`` runs every SQL call on a private background
``threading.Thread`` that is created with the stdlib default ``daemon=False``.
When a test opens a connection (directly via ``EpisodicMemory`` /
``RelationshipMemory`` or transitively via ``PersonaAgent.initialize_memory()``)
and the test forgets to call ``close()``, the worker thread keeps blocking
on its internal ``SimpleQueue.get()`` forever.  Python's interpreter
shutdown waits for all non-daemon threads to join before running ``atexit``
handlers, so the whole pytest process hangs after the last test reports
``passed`` — even though every assertion succeeded.

Daemonising the worker means a leaked connection no longer blocks process
exit: the daemon thread is killed when the interpreter tears down.
Properly-closed connections are unaffected (the worker drains its sentinel
and exits normally before the process ends).

This is a *test-only* safety net.  Production code paths still close their
connections in their own ``close()`` methods (see ``EpisodicMemory.close``,
``RelationshipMemory.close``).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_logger = logging.getLogger(__name__)


# ─── golden-trace strip tests (RFC 0044) ─────────────────────────────────────
#
# A "strip test" replays a committed golden under a changed posture and
# expects the cassette to MISS: that is what makes the golden's request-hash
# pin load-bearing rather than the mock-authored transcript. Every seed
# suite stages the same way, so the staging and the verdict live here.


def stage_recipe_override(
    recipe_path: Path, tmp_path: Path, mutate: Callable[[dict[str, Any]], None],
) -> Path:
    """Copy one eval recipe into ``tmp_path`` with ``mutate`` applied to the
    loaded document, and its golden sidecar beside it unchanged. Returns
    ``tmp_path`` — pass it as the runner's ``--eval-sets-dir``."""
    from evaluators.runner import golden_path_for  # pulls ``agents``; keep lazy

    recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    mutate(recipe)
    (tmp_path / recipe_path.name).write_text(
        yaml.safe_dump(recipe), encoding="utf-8",
    )
    golden = golden_path_for(recipe_path)
    shutil.copy(golden, tmp_path / golden.name)
    return tmp_path


def assert_cassette_miss(
    result: subprocess.CompletedProcess[str], why: str,
) -> None:
    """The strip-test verdict: the replay failed, and failed on a cassette
    miss. The miss surfaces through the runtime's LLM-error wrapping, so
    this matches the ``ReplayCassetteMissError`` message, not the class."""
    assert result.returncode != 0, f"{why}:\n{result.stdout}\n{result.stderr}"
    assert "no recorded response for request" in result.stderr, (
        result.stdout, result.stderr,
    )


def daemonize_aiosqlite_workers() -> None:
    """Patch ``aiosqlite.core.Connection.__init__`` so its worker thread is daemon.

    Idempotent — safe to call from multiple conftests.  No-op if the patch is
    already installed or if ``aiosqlite`` is not importable.
    """
    try:
        import aiosqlite.core as _core
    except ImportError:
        return

    if getattr(_core.Connection.__init__, "_persatrix_daemon_patched", False):
        return

    _orig_init = _core.Connection.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _orig_init(self, *args, **kwargs)
        # ``self._thread`` is created (but not started) inside the original
        # ``__init__``; mark it daemon before the connection is awaited.
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.daemon = True
        else:
            # Defensive: aiosqlite refactored away the private ``_thread``
            # attribute.  Surface a warning instead of silently no-opping
            # so the next pytest hang gets a hint at the root cause.
            _logger.warning(
                "aiosqlite.Connection has no '_thread' attribute — "
                "daemonisation patch is a no-op. The pytest-hang guard "
                "in tests/_test_infra.py needs updating for the current "
                "aiosqlite version.",
            )

    _patched_init._persatrix_daemon_patched = True  # type: ignore[attr-defined]
    _core.Connection.__init__ = _patched_init  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def isolate_optimization_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start every test from the shipped ``config/optimization.yaml``.

    Since v0.3.16 K1 every persona construction reads that file
    (``memory_knobs.resolve_memory_knobs`` → ``memory_budget.tokens``), so
    a ``PERSATRIX_OPTIMIZATION_CONFIG`` exported in a developer's shell —
    an EXP-001 arm file that sets the budget, say — would silently re-budget
    or fail every test that builds a persona.  Drop the pin and clear the
    process-wide parse cache before and after each test; a test that wants
    a specific file sets the variable itself (its ``monkeypatch`` runs
    after this one) and the seed-replay suites pass it to their subprocess
    environment, which this fixture never touches.
    """
    from agents.optimization import reset_cache

    monkeypatch.delenv("PERSATRIX_OPTIMIZATION_CONFIG", raising=False)
    reset_cache()
    yield
    reset_cache()


# ─── CI-wiring pins (shared by the gate-pin tests) ───────────────────────────
#
# Several unit tests pin a merge gate by reading the Makefile and ci.yml as
# text: the golangci-lint pin (v0.3.16 PR C1) and the eval-replay gate (PR C2).
# They each carried a private copy of the same two readers, and the copies
# shared one bug — a recipe regex compiled with ``re.S``, under which ``.*``
# crosses newlines and the "recipe body" ran to the end of the Makefile, so
# an assertion on the body passed for a fragment sitting in any later
# target. One reader here, line-anchored, so the next pin cannot re-copy it.

REPO_ROOT = Path(__file__).resolve().parents[1]


def makefile_recipe_body(target: str) -> str:
    """The tab-indented recipe lines of one Make ``target`` — nothing past them.

    Anchored per line (``[^\n]*``, no ``re.S``), so the captured body ends at
    the first line that is not a recipe line; a fragment in a later target is
    not in it.
    """
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", text, re.M)
    if m is None:
        raise AssertionError(f"no `{target}` recipe in the Makefile")
    return m.group(1)


def ci_job_steps(job: str) -> list[dict[str, Any]]:
    """The ordered ``steps`` of one job in ``.github/workflows/ci.yml``."""
    workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return list(ci["jobs"][job]["steps"])
