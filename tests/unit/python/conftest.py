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
