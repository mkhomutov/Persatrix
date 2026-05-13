import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.tools.registry import clear_registry

# Re-export the catch-up loopback orchestrator as a session-scoped
# fixture name so test files in this directory can request
# ``orchestrator`` without importing it. Without this, importing the
# fixture by name into each test file triggers ruff F811 (fixture
# parameters look like redefinitions of the imported name to ruff's
# scope analysis) — twelve false-positive findings across the
# catch-up suite. ``noqa: F401`` is the documented escape hatch for
# the pytest fixture-discovery pattern. (PR-265 review follow-up:
# extracted fixture to keep test_channel_catchup.py under the
# 500-line review cap after adding the timestamp / explicit-limit
# tests.)
from ._catchup_test_helpers import orchestrator  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _isolate_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``PERSATRIX_SESSION_ID`` does not leak across tests.

    RFC 0031 Phase 1 makes :class:`agents.memory.facade.MemoryFacade` read
    ``PERSATRIX_SESSION_ID`` at construction so the task-agent /
    sub-agent path inherits the operator-namespace tag without an
    explicit kwarg at every write site
    (see ``agents/memory/facade.py`` ``__init__`` for the rationale).

    The flip side: any test that constructs a ``MemoryFacade`` and
    asserts ``_session_id == "legacy"`` will silently pick up a shell-
    inherited value when the developer (or a CI job) happens to have
    ``PERSATRIX_SESSION_ID`` exported.  This autouse fixture
    monkeypatches the var out before every test so the env baseline is
    deterministic; tests that *want* the env-read path call
    ``monkeypatch.setenv("PERSATRIX_SESSION_ID", ...)`` themselves and
    that overrides this delete for the test's scope.

    Symmetric with ``_clean_registry`` above (same autouse pattern, same
    "deterministic baseline before the test runs" intent).  PR 4 review
    follow-up F5.
    """
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)


@pytest.fixture
async def memory():
    """Create an initialized EpisodicMemory instance with in-memory DB."""
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
async def memory_pair():
    """Create two EpisodicMemory instances sharing different agent IDs on the same DB.

    Uses a temp file so both connections share state.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        mem_a = EpisodicMemory(agent_id="agent-a", db_path=path)
        mem_b = EpisodicMemory(agent_id="agent-b", db_path=path)
        await mem_a.initialize()
        await mem_b.initialize()
        yield mem_a, mem_b
        await mem_a.close()
        await mem_b.close()
    finally:
        os.unlink(path)
