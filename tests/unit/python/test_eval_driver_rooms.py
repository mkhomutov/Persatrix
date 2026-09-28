"""RFC 0049 PR 4 — the eval driver's multi-room + memory-override extensions.

Two recipe-format extensions feed the cross-room seeds:

* per-interaction ``room:`` — the driver binds the value as the event's
  ``persatrix_session`` metadata (production's per-(agent, channel) session
  binding), so memory written during the turn stamps that RFC 0031 session;
  room-less interactions never set the key, keeping the landed single-room
  goldens byte-identical.
* ``setup.memory`` — a deep-merge override into the resolved persona
  config's ``memory`` block, so a recipe pins runtime knobs (the cross-room
  posture) independently of the shipped default; the ``:memory:`` db-path
  force always wins over it.

Self-contained (the ``test_eval_persona_driver`` harness shape, duplicated —
unit test modules don't cross-import); the room-binding leg drives the real
``PersonaRuntimeDriver.run`` loop against a FAKE agent captured through the
``create_persona_agent`` seam, so the assertion is on the metadata the loop
actually stamps per event.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agents.session_id import EVENT_SESSION_METADATA_KEY
from evaluators.eval_set import load_eval_set
from evaluators.persona_driver import (
    PersonaRuntimeDriver,
    _merge_memory_overrides,
)

_CONFIG: dict[str, Any] = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "model": "claude-sonnet-4-6",
    "memory": {"db_path": "data/memory.db", "facts": {"enabled": True}},
}


# ─── setup.memory deep-merge ─────────────────────────────────────────────────


class TestMergeMemoryOverrides:
    def test_nested_merge_preserves_siblings(self):
        config = {"memory": {"db_path": "x", "facts": {"enabled": True}}}
        _merge_memory_overrides(config, {"facts": {"cross_room": "shadow"}})
        assert config["memory"]["facts"] == {
            "enabled": True, "cross_room": "shadow",
        }
        assert config["memory"]["db_path"] == "x"

    def test_creates_memory_block_when_absent(self):
        config: dict[str, Any] = {}
        _merge_memory_overrides(config, {"episodic": {"cross_room": "off"}})
        assert config["memory"]["episodic"] == {"cross_room": "off"}

    def test_empty_override_is_a_noop(self):
        config = {"memory": {"db_path": "x"}}
        _merge_memory_overrides(config, {})
        assert config == {"memory": {"db_path": "x"}}

    def test_db_path_force_wins_over_an_override(self, tmp_path: Path):
        """Golden portability is non-negotiable: even a recipe that names a
        ``db_path`` records against ``:memory:`` (the force runs after the
        merge in ``PersonaRuntimeDriver.run``)."""
        from evaluators.persona_driver import _force_in_memory_db

        config = {"memory": {"db_path": "x"}}
        _merge_memory_overrides(config, {"db_path": str(tmp_path / "evil.db")})
        _force_in_memory_db(config)
        assert config["memory"]["db_path"] == ":memory:"


# ─── per-interaction room binding ────────────────────────────────────────────


_ROOMED = textwrap.dedent(
    """
    id: EVAL-MEMORY-002
    title: room-binding drive
    setup:
      persona: ember-owl
      user: sam
    interactions:
      - id: i1
        room: dm-sam
        turns:
          - user: "teach turn"
      - id: i2
        room: standup
        turns:
          - user: "ask turn"
      - id: i3
        turns:
          - user: "room-less turn"
          - assistant: {match: contains, value: "ok"}
    """
).strip()


class _FakeAgent:
    """Captures every event the driver loop dispatches; enough surface for
    ``PersonaRuntimeDriver.run`` + ``_snapshot_state``."""

    def __init__(self) -> None:
        self.events: list[Any] = []

        class _Rel:
            async def get_all_relationships(self):
                return []

        class _Memory:
            relationship = _Rel()

        self.memory = _Memory()

    async def initialize_memory(self) -> None:
        pass

    async def close_memory(self) -> None:
        pass

    def set_history_fetcher(self, fetcher: Any) -> None:
        pass

    async def on_event(self, event: Any) -> list[Any]:
        self.events.append(event)
        return []

    async def drain_pending_summaries(self) -> None:
        pass


async def test_room_binds_session_metadata_per_interaction(tmp_path: Path) -> None:
    """Roomed interactions carry their room as ``persatrix_session``; the
    room-less interaction never sets the key — the landed single-room
    goldens' event shape is untouched by the extension."""
    p = tmp_path / "recipe.yaml"
    p.write_text(_ROOMED, encoding="utf-8")
    eval_set = load_eval_set(p)

    fake = _FakeAgent()
    driver = PersonaRuntimeDriver(config_resolver=lambda name: dict(_CONFIG))
    with patch("agents.persona.create_persona_agent", return_value=fake):
        await driver.run(eval_set, provider=object())

    sessions = [e.metadata.get(EVENT_SESSION_METADATA_KEY) for e in fake.events]
    assert sessions == ["dm-sam", "standup", None]
    assert EVENT_SESSION_METADATA_KEY not in fake.events[2].metadata
    # The pre-extension metadata shape rides along unchanged.
    assert all(
        e.metadata["channel_classification"] == "internal" for e in fake.events
    )
