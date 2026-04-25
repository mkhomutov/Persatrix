import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.tools.registry import clear_registry


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
