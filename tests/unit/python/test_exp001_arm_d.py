"""EXP-001 harness — arm D's pieces: its channels, stores and restarts (PR 5c).

Arm D keeps one deployment for a series, so its advisers' memory carries
from meeting to meeting. The harness declares a channel for every meeting,
puts each adviser's persona state back to the runtime's defaults before a
start, and checks that a restart leaves memory exactly as it was (check 2):
the catch-up pass replayed nothing, and every table but the saved state
holds what it held. Before a meeting's first try it keeps a copy of the
stores, which a try held again starts from.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from agents.persona import create_persona_agent
from evaluators.exp001.arm_d import (
    catch_up_replayed,
    keep_stores,
    memory_rows,
    reset_persona_state,
    restore_stores,
    series_channels,
)
from evaluators.exp001.deployment import REPO, Layout, adviser_config, channel_config
from evaluators.exp001.materials import load_series
from evaluators.exp001.panel import load_panel

from ._persona_test_helpers import _make_client

_EXP = REPO / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
SERIES = load_series(_EXP / "series-1.yaml")
CHAIR = PANEL.chair.id


def _store(db: Path, *episodes: str, state: str | None = '{"energy": 0.4}', count: int = 3) -> None:
    """A store shaped like an adviser's: its saved state, and some memory."""
    db.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(db)) as store, store:
        store.execute(
            "CREATE TABLE IF NOT EXISTS agent_state (agent_id TEXT PRIMARY KEY, "
            "interaction_count INTEGER, persona_state_json TEXT, updated_at REAL)",
        )
        store.execute("CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, summary TEXT)")
        store.execute(
            "INSERT OR REPLACE INTO agent_state VALUES (?, ?, ?, 1.0)", (db.stem, count, state),
        )
        rows = [(e, f"said {e}") for e in episodes]
        store.executemany("INSERT INTO episodes VALUES (?, ?)", rows)


def _state(db: Path) -> tuple[int, str | None]:
    with contextlib.closing(sqlite3.connect(db)) as store:
        row = store.execute(
            "SELECT interaction_count, persona_state_json FROM agent_state WHERE agent_id = ?",
            (db.stem,),
        ).fetchone()
    return row[0], row[1]


class TestSeriesChannels:
    def test_every_meeting_has_a_channel_named_for_its_place(self) -> None:
        channels = series_channels(PANEL, SERIES)
        assert [c["name"] for c in channels] == [
            f"advice-{n}" for n in range(1, len(SERIES.meetings) + 1)
        ]

    def test_each_is_shaped_as_the_arms_one_channel(self) -> None:
        """B and C name and shape theirs the same way, one per deployment."""
        assert series_channels(PANEL, SERIES)[2] == channel_config(
            PANEL, "D", name="advice-3", organisation=SERIES.organisation,
        )


class TestStores:
    def test_a_copy_puts_back_the_stores_as_they_were(self, tmp_path: Path) -> None:
        layout = Layout(tmp_path / "deployment")
        _store(layout.memory_db(CHAIR), "briefing")
        layout.data.mkdir()
        (layout.data / "channels.db").write_bytes(b"one meeting")
        keep_stores(layout, tmp_path / "before")
        _store(layout.memory_db(CHAIR), "a failed try")
        (layout.data / "channels.db").write_bytes(b"one meeting and a failed try")
        restore_stores(layout, tmp_path / "before")
        assert memory_rows(layout.memory_db(CHAIR)) == {
            "episodes": (repr(("briefing", "said briefing")),),
        }
        assert (layout.data / "channels.db").read_bytes() == b"one meeting"

    def test_a_store_made_since_the_copy_is_dropped(self, tmp_path: Path) -> None:
        """The briefing's copy is of empty stores: a try held again starts as the first did."""
        layout = Layout(tmp_path / "deployment")
        layout.memory.mkdir(parents=True)
        layout.data.mkdir(parents=True)
        keep_stores(layout, tmp_path / "before")
        _store(layout.memory_db(CHAIR), "a failed try")
        (layout.data / "channels.db-wal").write_bytes(b"pages")
        restore_stores(layout, tmp_path / "before")
        assert list(layout.memory.iterdir()) == []
        assert list(layout.data.iterdir()) == []

    def test_only_the_stores_are_copied(self, tmp_path: Path) -> None:
        """The orchestrator's logs and audit trail are not stores."""
        layout = Layout(tmp_path / "deployment")
        _store(layout.memory_db(CHAIR))
        (layout.data / "logs").mkdir(parents=True)
        (layout.data / "audit.jsonl").write_text("{}\n")
        (layout.data / "accounts.db").write_bytes(b"accounts")
        keep_stores(layout, tmp_path / "before")
        before = tmp_path / "before"
        kept = sorted(str(p.relative_to(before)) for p in before.rglob("*") if p.is_file())
        assert kept == ["data/accounts.db", f"memory/{CHAIR}.db"]

    def test_a_copy_already_kept_is_refused(self, tmp_path: Path) -> None:
        """An earlier run's copy would be put back in place of this one's."""
        layout = Layout(tmp_path / "deployment")
        (tmp_path / "before").mkdir()
        with pytest.raises(FileExistsError):
            keep_stores(layout, tmp_path / "before")

    def test_no_copy_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="before"):
            restore_stores(Layout(tmp_path / "deployment"), tmp_path / "before")


class TestPersonaState:
    def test_the_persona_state_goes_back_to_the_defaults_and_the_counter_stays(
        self, tmp_path: Path,
    ) -> None:
        """The counter paces note reflection, which memory as shipped keeps doing."""
        layout = Layout(tmp_path)
        _store(layout.memory_db(CHAIR), "briefing", count=3)
        held = memory_rows(layout.memory_db(CHAIR))
        reset_persona_state(layout, PANEL)
        assert _state(layout.memory_db(CHAIR)) == (3, None)
        assert memory_rows(layout.memory_db(CHAIR)) == held

    def test_an_adviser_without_a_store_is_passed_over(self, tmp_path: Path) -> None:
        reset_persona_state(Layout(tmp_path), PANEL)
        assert not (tmp_path / "memory").exists()

    async def test_the_runtime_starts_from_its_defaults(self, tmp_path: Path) -> None:
        layout = Layout(tmp_path)
        layout.memory.mkdir()
        entry = adviser_config(PANEL, PANEL.chair, memory_db=layout.memory_db(CHAIR))
        agent = create_persona_agent(agent_id=CHAIR, config=entry, llm_client=_make_client())
        await agent.initialize_memory()
        agent._state.drain_energy()
        await agent.close_memory()  # which saves the drained energy
        reset_persona_state(layout, PANEL)
        agent = create_persona_agent(agent_id=CHAIR, config=entry, llm_client=_make_client())
        await agent.initialize_memory()
        try:
            assert agent._state.energy == 1.0
        finally:
            await agent.close_memory()


class TestMemoryRows:
    def test_no_store_holds_nothing_to_compare(self, tmp_path: Path) -> None:
        assert memory_rows(tmp_path / "missing.db") is None

    def test_the_saved_state_is_left_out(self, tmp_path: Path) -> None:
        db = tmp_path / f"{CHAIR}.db"
        _store(db, "briefing")
        before = memory_rows(db)
        with contextlib.closing(sqlite3.connect(db)) as store, store:
            store.execute("UPDATE agent_state SET persona_state_json = '{}', updated_at = 2.0")
        assert memory_rows(db) == before

    def test_a_row_added_or_changed_shows(self, tmp_path: Path) -> None:
        db = tmp_path / f"{CHAIR}.db"
        _store(db, "briefing")
        before = memory_rows(db)
        with contextlib.closing(sqlite3.connect(db)) as store, store:
            store.execute("UPDATE episodes SET summary = 'said it twice'")
        assert memory_rows(db) != before

    async def test_the_runtime_reopening_a_store_leaves_its_memory_as_it_was(
        self, tmp_path: Path,
    ) -> None:
        """Only the saved state changes when an adviser opens and closes its store."""
        db = tmp_path / f"{CHAIR}.db"
        entry = adviser_config(PANEL, PANEL.chair, memory_db=db)

        async def open_and_close() -> None:
            agent = create_persona_agent(agent_id=CHAIR, config=entry, llm_client=_make_client())
            await agent.initialize_memory()
            await agent.close_memory()

        await open_and_close()
        first = memory_rows(db)
        await open_and_close()
        assert first is not None and memory_rows(db) == first


def _log(path: Path, *messages: str) -> Path:
    path.write_text("".join(
        json.dumps({"timestamp": "2026-10-01T09:00:00Z", "level": "info", "message": m}) + "\n"
        for m in messages
    ) + "not JSON\n")
    return path


class TestCatchUp:
    def test_the_messages_the_pass_replayed_are_read_from_the_log(self, tmp_path: Path) -> None:
        log = _log(
            tmp_path / f"{CHAIR}.log",
            "channels: catch-up complete agent=velvet-pika channels=6 events=9 elapsed_ms=3",
            f"channels: catch-up complete agent={CHAIR} channels=6 events=0 elapsed_ms=12",
        )
        assert catch_up_replayed(log, CHAIR) == 0

    def test_before_the_pass_ends_there_is_no_count(self, tmp_path: Path) -> None:
        assert catch_up_replayed(tmp_path / "missing.log", CHAIR) is None
        log = _log(tmp_path / f"{CHAIR}.log", "channels: catch-up replay aborted for agent x")
        assert catch_up_replayed(log, CHAIR) is None

    def test_the_line_is_the_one_the_runtime_logs(self) -> None:
        source = (REPO / "agents" / "channel_catchup.py").read_text()
        assert '"channels: catch-up complete agent=%s channels=%d events=%d "' in source
