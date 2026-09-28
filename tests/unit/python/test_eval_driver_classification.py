"""RFC 0037 PR 8 — the eval driver's per-interaction classification extension.

The confidentiality seed (``EVAL-MEMORY-004``) needs interactions that ACT at
declared §A levels: teach in a ``restricted`` room, ask in an ``internal`` one,
ask back at ``restricted``. The recipe format gains an optional per-interaction
``classification:`` whose value the driver seeds as the event's
``channel_classification`` wire stamp — the same key the orchestrator's
dispatch path stamps, read by ``acting_classification_for_event`` on the §D
gate side and captured frozen-at-open on the §C write side.

The contract under test, both directions:

* a declared level reaches the event verbatim (the gate acts on it);
* an undeclared interaction keeps the driver's DM default ``internal`` — the
  landed goldens (EVAL-MEMORY-001/002/003, EVAL-WORKING-001) carry no
  ``classification:`` key and must stay byte-identical.

Self-contained (the ``test_eval_driver_rooms`` harness shape, duplicated —
unit test modules don't cross-import).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from evaluators.eval_set import load_eval_set
from evaluators.persona_driver import PersonaRuntimeDriver

_CONFIG: dict[str, Any] = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "model": "claude-sonnet-4-6",
    "memory": {"db_path": "data/memory.db", "facts": {"enabled": True}},
}


_CLASSIFIED = textwrap.dedent(
    """
    id: EVAL-MEMORY-004
    title: classification drive
    setup:
      persona: ember-owl
      user: sam
    interactions:
      - id: i1
        classification: restricted
        turns:
          - user: "teach turn"
      - id: i2
        classification: internal
        turns:
          - user: "ask turn"
      - id: i3
        turns:
          - user: "undeclared turn"
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


# ─── loader ──────────────────────────────────────────────────────────────────


class TestLoaderClassification:
    def test_declared_levels_parse_onto_the_interaction(self, tmp_path: Path):
        p = tmp_path / "recipe.yaml"
        p.write_text(_CLASSIFIED, encoding="utf-8")
        eval_set = load_eval_set(p)
        assert [i.classification for i in eval_set.interactions] == [
            "restricted", "internal", None,
        ]

    def test_off_vocabulary_level_is_rejected_at_load(self, tmp_path: Path):
        """The §A vocabulary is schema-enforced — a typo'd level must fail the
        load, not silently stamp a value ``acting_rank`` floors to ``public``
        (which would record a golden in the withhold-everything posture)."""
        p = tmp_path / "recipe.yaml"
        p.write_text(
            _CLASSIFIED.replace("classification: restricted",
                                "classification: restrictedd"),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="schema validation failed"):
            load_eval_set(p)


# ─── driver stamping ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_declared_classification_stamps_the_event(tmp_path: Path) -> None:
    """Declared levels reach the wire stamp verbatim; the undeclared
    interaction keeps the DM default ``internal`` — the pre-extension shape
    every landed golden was recorded under."""
    p = tmp_path / "recipe.yaml"
    p.write_text(_CLASSIFIED, encoding="utf-8")
    eval_set = load_eval_set(p)

    fake = _FakeAgent()
    driver = PersonaRuntimeDriver(config_resolver=lambda name: dict(_CONFIG))
    with patch("agents.persona.create_persona_agent", return_value=fake):
        await driver.run(eval_set, provider=object())

    stamps = [e.metadata["channel_classification"] for e in fake.events]
    assert stamps == ["restricted", "internal", "internal"]
