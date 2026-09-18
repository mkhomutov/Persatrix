"""``EpisodicMemory.reinforce`` — counting a use of chosen episodes (ISSUE-0163).

``access_count`` multiplies an episode's recall ranking, so each use
counted moves it up on later turns.  :meth:`EpisodicMemory.recall` counts
one for every row it returns.  ``reinforce`` counts one only for the ids
it is given, which lets the persona prompt path read first and count the
episodes its prompt actually carried.  These tests pin that it touches
exactly those rows and only the calling agent's.
"""

from __future__ import annotations

import time

from agents.memory.episodic import EpisodicMemory


async def test_counts_a_use_of_the_given_episodes_only(memory: EpisodicMemory):
    used = await memory.store_episode("atlas deployment retro", {})
    unused = await memory.store_episode("atlas budget review", {})

    before = time.time()
    await memory.reinforce([used])
    after = time.time()

    used_row = await memory.get_episode(used)
    unused_row = await memory.get_episode(unused)
    assert used_row is not None and used_row.access_count == 1
    assert used_row.last_accessed_at is not None
    assert before <= used_row.last_accessed_at <= after
    assert unused_row is not None and unused_row.access_count == 0
    assert unused_row.last_accessed_at is None


async def test_each_call_counts_one_more_use(memory: EpisodicMemory):
    ep_id = await memory.store_episode("atlas deployment retro", {})
    await memory.reinforce([ep_id])
    await memory.reinforce([ep_id])
    row = await memory.get_episode(ep_id)
    assert row is not None and row.access_count == 2


async def test_an_empty_list_writes_nothing(memory: EpisodicMemory):
    ep_id = await memory.store_episode("atlas deployment retro", {})
    await memory.reinforce([])
    row = await memory.get_episode(ep_id)
    assert row is not None and row.access_count == 0


async def test_another_agents_episode_is_untouched(
    memory_pair: tuple[EpisodicMemory, EpisodicMemory],
):
    """Two agents sharing one database: an id from agent A's store is
    not agent B's to reinforce."""
    mem_a, mem_b = memory_pair
    ep_id = await mem_a.store_episode("agent A's episode", {})
    await mem_b.reinforce([ep_id])
    row = await mem_a.get_episode(ep_id)
    assert row is not None and row.access_count == 0
