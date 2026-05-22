"""RFC 0024 PR 3b — default-off invariant for ``SalienceWake``.

The default ``autonomy.salience_threshold`` (``0.95``) is strictly above
PR 3a's conservative-scoring maximum (``REFLECTION_CONTRADICTION_SALIENCE
= 0.6``), so a stock-config persona under stock PR 3a scoring produces
**zero** ``SalienceWake`` enqueues over any time window — every write
suppresses with ``suppressed_reason="below_threshold"``.

This is the regression backstop the [RFC 0024 PR plan PR 3b
key implementation detail](../../../docs/rfcs/0024-pr-plan.md) names:
"Two constants must agree by inequality — `REFLECTION_CONTRADICTION_SALIENCE
< autonomy.salience_threshold default`."  Lowering one or raising the
other without re-pinning the invariant breaks the v0.3.3 release-gate
("Idle Truly Idle"), so the assertion is intentionally redundant with
:mod:`agents.tests.test_memory_write_event.TestReflectionContradictionConstant`
— the inequality is asserted from both sides.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.event_loop import EventLoop
from agents.memory._events import (
    MemoryWriteBus,
    MemoryWriteEvent,
    get_memory_write_bus,
    set_memory_write_bus,
)
from agents.memory._salience import (
    EPISODIC_APPEND_SALIENCE,
    FACTS_APPEND_SALIENCE,
    NOTES_APPEND_SALIENCE,
    REFLECTION_CONTRADICTION_SALIENCE,
    RELATIONSHIP_APPEND_SALIENCE,
)
from agents.observability import metrics as pmetrics
from agents.persona_types import ActionType, AgentAction, AgentEvent

# Threshold default ships in the schema; the EventLoop class-level default
# mirrors it.  The two constants must stay in sync — see
# :mod:`agents.event_loop`'s :attr:`EventLoop.DEFAULT_SALIENCE_THRESHOLD`.
_DEFAULT_THRESHOLD = 0.95
_AGENT_ID = "default-off-persona"


@pytest.fixture
def fresh_bus() -> Iterator[MemoryWriteBus]:
    original = get_memory_write_bus()
    bus = MemoryWriteBus()
    set_memory_write_bus(bus)
    try:
        yield bus
    finally:
        set_memory_write_bus(original)


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


async def _on_event(_e: AgentEvent) -> list[AgentAction]:
    return [AgentAction(ActionType.DO_NOTHING, {})]


async def _on_tick(_w: object) -> None:
    return None


@pytest.fixture
async def default_loop() -> AsyncIterator[EventLoop]:
    """EventLoop with no ``salience_*`` overrides — uses class defaults."""
    loop = EventLoop(
        agent_id=_AGENT_ID,
        on_event=_on_event,
        on_tick=_on_tick,  # type: ignore[arg-type]
    )
    loop.start()
    try:
        yield loop
    finally:
        await loop.stop(timeout=1.0)


def _collect_salience_total(reader: InMemoryMetricReader) -> dict[str, int]:
    data = reader.get_metrics_data()
    out: dict[str, int] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != "agent.wake.salience":
                    continue
                for dp in m.data.data_points:  # type: ignore[union-attr]
                    raw = (dp.attributes or {}).get("suppressed_reason")
                    reason = str(raw) if raw is not None else "<missing>"
                    out[reason] = out.get(reason, 0) + int(getattr(dp, "value", 0))
    return out


class TestDefaultThresholdConstant:
    def test_default_threshold_strictly_above_pr3a_max(self) -> None:
        """Class-level default must stay strictly above PR 3a's max scoring."""
        assert EventLoop.DEFAULT_SALIENCE_THRESHOLD > REFLECTION_CONTRADICTION_SALIENCE
        assert EventLoop.DEFAULT_SALIENCE_THRESHOLD == _DEFAULT_THRESHOLD

    def test_pr3a_max_is_below_threshold(self) -> None:
        """The 0.6 → 0.95 inequality is the v0.3.3 release-gate invariant.

        Asserted from this side too so a future tuning PR that raises the
        constant in :mod:`agents.memory._salience` (e.g. to ``1.0``) trips
        this test rather than silently flipping salience wakes from off to
        on under stock config.
        """
        assert REFLECTION_CONTRADICTION_SALIENCE < _DEFAULT_THRESHOLD


class TestSchemaDefaultMatchesCode:
    """The schema ``default`` and the ``EventLoop`` class constant are two
    sources of truth for the same value.

    ``jsonschema`` does not inject defaults, and
    :func:`agents.server_persona.initialize_persona_agents` reads
    ``autonomy.get("salience_threshold")`` / ``...rate_max_per_sec`` and
    falls back to the class constant when the key is absent — so the class
    constant is the *effective* runtime default and the schema ``default``
    is documentation.  Pin the two equal so a change to one without the
    other is caught here rather than silently diverging the documented
    schema from runtime behaviour.
    """

    @staticmethod
    def _autonomy_props() -> dict[str, Any]:
        schema = json.loads(
            Path("schemas/agent.schema.json").read_text(encoding="utf-8"),
        )
        # ``json.loads`` is typed ``Any``; pin the return through a typed
        # local so mypy's ``no-any-return`` (whole-package check) is satisfied.
        props: dict[str, Any] = schema["definitions"]["autonomy"]["properties"]
        return props

    def test_schema_threshold_default_matches_event_loop_constant(self) -> None:
        props = self._autonomy_props()
        assert (
            props["salience_threshold"]["default"]
            == EventLoop.DEFAULT_SALIENCE_THRESHOLD
        )

    def test_schema_rate_max_default_matches_event_loop_constant(self) -> None:
        props = self._autonomy_props()
        assert (
            props["salience_rate_max_per_sec"]["default"]
            == EventLoop.DEFAULT_SALIENCE_RATE_MAX_PER_SEC
        )


class TestDefaultOffInvariant:
    async def test_stream_of_pr3a_writes_produces_zero_wakes(
        self,
        fresh_bus: MemoryWriteBus,
        default_loop: EventLoop,
        metric_reader: InMemoryMetricReader,
    ) -> None:
        """A stream of stock-PR3a-scored writes records only ``below_threshold``."""
        # All five PR 3a per-tier constants (per ``agents.memory._salience``).
        # Reflection-contradiction (0.6) is the highest PR 3a writes today.
        constants = [
            ("episodic", EPISODIC_APPEND_SALIENCE),
            ("notes", NOTES_APPEND_SALIENCE),
            ("facts", FACTS_APPEND_SALIENCE),
            ("relationship", RELATIONSHIP_APPEND_SALIENCE),
            ("reflection", REFLECTION_CONTRADICTION_SALIENCE),
        ]
        for tier, sal in constants:
            for _ in range(5):
                fresh_bus.publish(
                    MemoryWriteEvent(
                        agent_id=_AGENT_ID,
                        tier=tier,  # type: ignore[arg-type]
                        salience=sal,
                        source_span_id=None,
                        written_at=time.time(),
                    ),
                )
        # Total writes: 25.  All must suppress; none must enqueue.
        await asyncio.sleep(0.05)
        reasons = _collect_salience_total(metric_reader)
        assert reasons.get("none", 0) == 0, (
            f"Default-off invariant breached — stock PR 3a writes produced "
            f"{reasons.get('none', 0)} SalienceWake enqueues. "
            "Re-verify that EventLoop.DEFAULT_SALIENCE_THRESHOLD > "
            "REFLECTION_CONTRADICTION_SALIENCE."
        )
        assert reasons.get("below_threshold", 0) == 25
