"""
Tests for EpisodicMemory — episode deletion/retention policy and future
migration forward-compatibility.
"""

import time
from unittest.mock import patch

import pytest

from agents.memory.episodic import EpisodicMemory

# ─── Episode deletion / retention ───────────────────────────


class TestDeleteOldEpisodes:
    """delete_old_episodes() removes compressed episodes past retention window."""

    async def test_deletes_compressed_old_episodes(self, memory: EpisodicMemory):
        """Old episodes with compression_level >= 1 are deleted."""
        db = memory._ensure_db()
        old_time = time.time() - 100 * 86400

        ep_id = await memory.store_episode(
            summary="Old compressed", context={},
        )
        await db.execute(
            "UPDATE episodes SET created_at = ?, compression_level = 1, "
            "compressed_at = ? WHERE id = ?",
            (old_time, old_time + 1000, ep_id),
        )
        await db.commit()

        deleted = await memory.delete_old_episodes(90)
        assert deleted == 1
        assert await memory.get_episode(ep_id) is None

    async def test_preserves_uncompressed_old_episodes(self, memory: EpisodicMemory):
        """Old episodes with compression_level=0 are NOT deleted."""
        db = memory._ensure_db()
        old_time = time.time() - 100 * 86400

        ep_id = await memory.store_episode(
            summary="Old but raw", context={},
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        deleted = await memory.delete_old_episodes(90)
        assert deleted == 0
        assert await memory.get_episode(ep_id) is not None

    async def test_preserves_recent_compressed_episodes(self, memory: EpisodicMemory):
        """Compressed episodes newer than threshold are NOT deleted."""
        db = memory._ensure_db()

        ep_id = await memory.store_episode(
            summary="Recent compressed", context={},
        )
        await db.execute(
            "UPDATE episodes SET compression_level = 1, compressed_at = ? WHERE id = ?",
            (time.time(), ep_id),
        )
        await db.commit()

        deleted = await memory.delete_old_episodes(90)
        assert deleted == 0
        assert await memory.get_episode(ep_id) is not None

    async def test_agent_scoped_deletion(self, memory_pair):
        """Only the calling agent's episodes are deleted."""
        mem_a, mem_b = memory_pair
        db_a = mem_a._ensure_db()
        db_b = mem_b._ensure_db()
        old_time = time.time() - 100 * 86400

        ep_a = await mem_a.store_episode(summary="Agent A old", context={})
        ep_b = await mem_b.store_episode(summary="Agent B old", context={})

        for db, ep_id in [(db_a, ep_a), (db_b, ep_b)]:
            await db.execute(
                "UPDATE episodes SET created_at = ?, compression_level = 1, "
                "compressed_at = ? WHERE id = ?",
                (old_time, old_time + 1000, ep_id),
            )
            await db.commit()

        deleted = await mem_a.delete_old_episodes(90)
        assert deleted == 1

        # Agent B's episode still exists
        assert await mem_b.get_episode(ep_b) is not None

    async def test_negative_older_than_days_raises(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="older_than_days must be >= 0"):
            await memory.delete_old_episodes(-5)

    async def test_empty_db_returns_zero(self, memory: EpisodicMemory):
        deleted = await memory.delete_old_episodes(90)
        assert deleted == 0

    async def test_mixed_compression_levels(self, memory: EpisodicMemory):
        """Only compression_level >= 1 episodes are eligible; level 0 preserved."""
        db = memory._ensure_db()
        old_time = time.time() - 100 * 86400

        raw_id = await memory.store_episode(summary="Raw old", context={})
        sum_id = await memory.store_episode(summary="Summarized old", context={})
        dist_id = await memory.store_episode(summary="Distilled old", context={})

        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, raw_id)
        )
        await db.execute(
            "UPDATE episodes SET created_at = ?, compression_level = 1, "
            "compressed_at = ? WHERE id = ?",
            (old_time, old_time + 1000, sum_id),
        )
        await db.execute(
            "UPDATE episodes SET created_at = ?, compression_level = 2, "
            "compressed_at = ? WHERE id = ?",
            (old_time, old_time + 2000, dist_id),
        )
        await db.commit()

        deleted = await memory.delete_old_episodes(90)
        assert deleted == 2  # summarized + distilled

        assert await memory.get_episode(raw_id) is not None
        assert await memory.get_episode(sum_id) is None
        assert await memory.get_episode(dist_id) is None

    async def test_retention_boundary(self, memory: EpisodicMemory):
        """Episode exactly at the boundary is NOT deleted (< cutoff)."""
        db = memory._ensure_db()

        # Pin wall-clock so cutoff arithmetic is deterministic.
        frozen_now = 1_000_000_000.0
        boundary_time = frozen_now - 90 * 86400

        ep_id = await memory.store_episode(summary="Boundary episode", context={})
        await db.execute(
            "UPDATE episodes SET created_at = ?, compression_level = 1, "
            "compressed_at = ? WHERE id = ?",
            (boundary_time, boundary_time + 1000, ep_id),
        )
        await db.commit()

        # With 90-day retention and a frozen clock, cutoff == boundary_time.
        # The SQL uses "created_at < cutoff" (strict), so the boundary
        # episode must be preserved.
        with patch("agents.memory.episodic_retention.time") as mock_time:
            mock_time.time.return_value = frozen_now
            deleted = await memory.delete_old_episodes(90)

        assert deleted == 0
        assert await memory.get_episode(ep_id) is not None


# ─── Future migration forward-compatibility (F-3a-3) ───────


class TestFutureMigration:
    async def test_hypothetical_v15_migration_applied(self):
        """Patch MIGRATIONS with a hypothetical v15 entry, verify v1–v15 applied.

        Forward-compat probe — always one past the highest real
        migration.  Bumped v14 → v15 when migration v14 (RFC 0031
        amendment — F-7 Option D, ISSUE-0093, PR D4: backfill contact
        notes onto relationship identity) landed; the rename + table-name
        bump preserves the "one-past-the-tail collision" contract that
        previously caught ``UNIQUE constraint failed: schema_version.version``
        regressions.
        """
        from agents.memory.migrations import MIGRATIONS

        v15 = (
            15,
            "Hypothetical test-only table",
            "CREATE TABLE IF NOT EXISTS _test_v15 (id TEXT PRIMARY KEY);",
        )
        original = list(MIGRATIONS)
        try:
            MIGRATIONS.append(v15)
            mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
            await mem.initialize()
            db = mem._ensure_db()

            # All fifteen versions should be recorded
            async with db.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ) as cursor:
                versions = [r[0] for r in await cursor.fetchall()]
            assert versions == [
                1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
            ]

            # v15 table should exist
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='_test_v15'"
            ) as cursor:
                assert await cursor.fetchone() is not None

            await mem.close()
        finally:
            MIGRATIONS.clear()
            MIGRATIONS.extend(original)
