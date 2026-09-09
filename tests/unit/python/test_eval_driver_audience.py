"""ISSUE-0132 (v0.3.16 PR A2) — the driver's audience-seed extensions.

Three additions, each load-bearing for EVAL-MEMORY-005 and each inert for
every landed golden: the in-process roster seam (``setup.rosters``), the
per-interaction ``channel`` override, and the group-turn
``respond_policy`` stamp. What this module pins is both halves — that the
additions do what the seed needs, and that a recipe declaring none of
them drives exactly the pre-A2 path (the byte-identity claim, from the
harness side).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from evaluators.eval_set import load_eval_set
from evaluators.persona_driver import PersonaRuntimeDriver

pytestmark = pytest.mark.asyncio

_CONFIG: dict[str, Any] = {"id": "ember-owl", "name": "Ember Owl"}

_AUDIENCE = textwrap.dedent("""
    id: EVAL-MEMORY-005
    title: audience harness
    setup:
      persona: ember-owl
      user: alice
      channel: "group:default"
      rosters:
        "group:standup": [alice, ember-owl, bob]
        "dm:alice:ember-owl": [alice, ember-owl]
    interactions:
      - id: i1-dm
        channel: "dm:alice:ember-owl"
        turns:
          - user: "teach"
          - assistant: {match: contains, value: "ok"}
      - id: i2-group
        channel: "group:standup"
        turns:
          - user: "ask in front of bob"
      - id: i3-default
        turns:
          - user: "no override — setup.channel"
""").strip()

_BARE = textwrap.dedent("""
    id: EVAL-MEMORY-001
    title: pre-A2 harness
    setup:
      persona: ember-owl
      user: alice
    interactions:
      - id: i1
        turns:
          - user: "channel-less turn"
          - assistant: {match: contains, value: "ok"}
""").strip()


class _FakeAgent:
    """Captures what the driver hands the runtime; enough surface for
    ``PersonaRuntimeDriver.run`` + ``_snapshot_state``."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.roster_fetchers: list[Any] = []

        class _Rel:
            async def get_all_relationships(self):  # noqa: ANN202
                return []

        class _Memory:
            relationship = _Rel()

        self.memory = _Memory()

    async def initialize_memory(self) -> None: ...
    async def close_memory(self) -> None: ...
    async def drain_pending_summaries(self) -> None: ...

    def set_history_fetcher(self, fetcher: Any) -> None: ...

    def set_roster_fetcher(self, fetcher: Any) -> None:
        self.roster_fetchers.append(fetcher)

    async def on_event(self, event: Any) -> list[Any]:
        self.events.append(event)
        return []


async def _drive(tmp_path: Path, recipe: str) -> _FakeAgent:
    p = tmp_path / "recipe.yaml"
    p.write_text(recipe, encoding="utf-8")
    fake = _FakeAgent()
    driver = PersonaRuntimeDriver(config_resolver=lambda _name: dict(_CONFIG))
    with patch("agents.persona.create_persona_agent", return_value=fake):
        await driver.run(load_eval_set(p), provider=object())
    return fake


async def test_declaring_rosters_wires_the_in_process_fetcher(
    tmp_path: Path,
) -> None:
    fake = await _drive(tmp_path, _AUDIENCE)
    assert len(fake.roster_fetchers) == 1
    assert await fake.roster_fetchers[0].fetch_members("group:standup") == {
        "id": "group:standup", "name": "group:standup",
        "members": [{"id": "alice"}, {"id": "ember-owl"}, {"id": "bob"}],
    }
    # A room the recipe did not declare is the deliberate fetch-failed
    # lever, not an empty room.
    assert await fake.roster_fetchers[0].fetch_members("group:ghost") is None


async def test_declaring_none_wires_no_fetcher_at_all(tmp_path: Path) -> None:
    """Every pre-v0.3.16 seed: no roster fetcher, so the audience check
    resolves nothing and the run is the pre-A2 path exactly."""
    fake = await _drive(tmp_path, _BARE)
    assert fake.roster_fetchers == []


async def test_per_interaction_channel_overrides_setup_channel(
    tmp_path: Path,
) -> None:
    """An entry's RFC 0037 §C provenance is the event's CHANNEL, so
    teaching and asking must be able to differ; an interaction that
    declares none stays on ``setup.channel``."""
    fake = await _drive(tmp_path, _AUDIENCE)
    assert [e.channel_id for e in fake.events] == [
        "dm:alice:ember-owl", "group:standup", "group:default",
    ]


async def test_group_turns_carry_a_respond_policy_and_dms_do_not(
    tmp_path: Path,
) -> None:
    """The RFC 0011 §D response gate overrides a ``dm:`` channel to
    ``always`` and bypasses a channel-less event, so only the group stamp
    is needed — which is exactly why every landed seed is untouched."""
    fake = await _drive(tmp_path, _AUDIENCE)
    assert [e.payload.get("respond_policy") for e in fake.events] == [
        None, "always", "always",
    ]
    bare = await _drive(tmp_path, _BARE)
    assert "respond_policy" not in bare.events[0].payload
