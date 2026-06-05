"""Process agent-id resolution for observability (v0.3.7 conversation
test-findings PR plan, F-5).

The tool-invocation metric (`agent_tool_invocations_total` /
`agent_tool_duration`) is recorded through the generic tool registry,
which has no persona context and labels the metric with
``current_agent_id()`` — a process-global that reads the
``PERSATRIX_AGENT_ID`` env var, defaulting to ``"unknown"``. Agents are
launched with the ``--agent <id>`` CLI flag (not the env var), so the env
var is unset and every tool-metric series carried ``agent.id="unknown"`` —
the broken label that hid memory-tool usage during the F-3 investigation.
(The persona-runtime paths that hold ``self.agent_id`` — the
``agent.persona.event`` span, ``event_dispatched`` — already label
correctly; only the generic registry path was wrong.)

Fix: a ``set_current_agent_id`` setter, called once at agent startup from
the resolved ``--agent`` id, so ``current_agent_id()`` returns the real
id for every process-global consumer (tool metric included). These tests
pin the setter contract; the startup wiring is a one-liner in
``server_cli``.
"""

from __future__ import annotations

import pytest

from agents.observability import metrics
from agents.observability.metrics import (
    current_agent_id,
    set_current_agent_id,
    tool_attrs,
)


@pytest.fixture(autouse=True)
def _reset_agent_id_cache():
    """Isolate the module-global agent-id cache across tests."""
    saved = metrics._AGENT_ID
    metrics._AGENT_ID = None
    yield
    metrics._AGENT_ID = saved


class TestSetCurrentAgentId:
    def test_setter_is_reflected(self) -> None:
        set_current_agent_id("ember-owl")
        assert current_agent_id() == "ember-owl"

    def test_setter_overrides_a_cached_value(self) -> None:
        """The lazy cache may already hold "unknown" (a read before
        startup wiring); the setter must override it, not no-op.
        """
        # Force the lazy cache to populate (env unset → "unknown").
        assert current_agent_id() == "unknown"
        set_current_agent_id("iron-fox")
        assert current_agent_id() == "iron-fox"

    def test_blank_id_normalizes_to_unknown(self) -> None:
        set_current_agent_id("   ")
        assert current_agent_id() == "unknown"

    def test_metric_attrs_carry_the_set_id(self) -> None:
        """End of the chain: the tool-metric attributes built from
        ``current_agent_id()`` carry the real id, not "unknown".
        """
        set_current_agent_id("nova-sparrow")
        attrs = tool_attrs(
            agent_id=current_agent_id(), tool_name="store_note", success=True,
        )
        assert attrs["agent.id"] == "nova-sparrow"
