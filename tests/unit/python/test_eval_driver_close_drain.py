"""The eval driver settles each turn before dispatching the next.

The RFC 0020 Phase-2 close finalisation (summarise + facts write) is a
fire-and-forget task (``close_path.py`` ``create_task``), and its
aiosqlite work runs on a worker thread. A recorded golden bakes in the
"finalisation completed before the next turn" interleaving (see
``EVAL-MEMORY-003``'s own description: the facts extract "BEFORE the
asking turn runs"), but nothing *enforced* it — on a contended runner
the task loses the race and the next turn's request misses the cassette
(the 2026-07-28 ``main`` CI failure). The driver's per-turn
``drain_pending_summaries()`` await is that enforcement; this pins it
with a fake agent whose "close" tasks are deliberately slow, so the
settled order can never happen by scheduling luck.

The adversarial real-runtime repro (delayed finalisation + a real
golden replay) is ``tests/integration/test_eval_driver_close_drain.py``.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

from evaluators.eval_set import load_eval_set
from evaluators.persona_driver import PersonaRuntimeDriver

_CONFIG: dict[str, Any] = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "model": "claude-sonnet-4-6",
    "memory": {"db_path": "data/memory.db"},
}

_THREE_TURNS = textwrap.dedent(
    """
    id: EVAL-MEMORY-004
    title: close-drain ordering drive
    setup:
      persona: ember-owl
      user: sam
    interactions:
      - id: i1
        turns:
          - user: "teach"
      - id: i2
        turns:
          - user: "bridge"
      - id: i3
        turns:
          - user: "ask"
          - assistant: {match: contains, value: "ok"}
    """
).strip()


class _RacingFakeAgent:
    """Every turn spawns a deliberately slow background "close" task —
    the ``close_path.py`` fire-and-forget shape. ``log`` records event
    dispatches and task completions in real order."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self._turn = 0
        self._pending: set[asyncio.Task[None]] = set()

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

    async def _slow_close(self, turn: int) -> None:
        # Slow enough that it can never win the inter-turn race by luck:
        # only an explicit drain lands it on the log before the next
        # dispatch (the driver loop itself has no other await between
        # turns).
        await asyncio.sleep(0.02)
        self.log.append(f"close[{turn}]")

    async def on_event(self, event: Any) -> list[Any]:
        self.log.append(f"event[{self._turn}]")
        task = asyncio.create_task(self._slow_close(self._turn))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        self._turn += 1
        return []

    async def drain_pending_summaries(self) -> None:
        await asyncio.gather(*list(self._pending), return_exceptions=True)


async def test_each_turn_settles_before_the_next_dispatches(
    tmp_path: Path,
) -> None:
    """Every turn's background close completes before the next turn's
    event dispatches — the interleaving the goldens are recorded under,
    now a contract instead of a coin flip."""
    p = tmp_path / "recipe.yaml"
    p.write_text(_THREE_TURNS, encoding="utf-8")
    eval_set = load_eval_set(p)

    fake = _RacingFakeAgent()
    driver = PersonaRuntimeDriver(config_resolver=lambda name: dict(_CONFIG))
    with patch("agents.persona.create_persona_agent", return_value=fake):
        await driver.run(eval_set, provider=object())

    assert fake.log == [
        "event[0]", "close[0]",
        "event[1]", "close[1]",
        "event[2]", "close[2]",
    ], fake.log
