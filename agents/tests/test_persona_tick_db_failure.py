"""The tier-exception swallowing contract on a persona TICK.

Split from ``test_persona_tick_shortcircuit.py`` when that file reached the
500-line review-friendly cap.  Shared helpers live in
``_persona_tick_helpers.py`` so neither half re-declares them.

The test body below is unchanged from the file it came out of.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tests._persona_tick_helpers import make_agent, make_client


class TestDBFailurePath:
    """All memory-tier lookups raise → admitted=0 → tick suppressed.

    This class closes the coverage gap flagged in the PR #149 deep review
    (Nice-to-have #1).  The short-circuit tests in the sibling file patch
    ``_inject_memory_context`` directly to return a zero-token result,
    which proves the *guard* fires on a zero result but does not prove
    the *upstream swallowing path* actually produces a zero result when
    every tier lookup raises.

    RFC 0017 §F documents — and ``_on_event_inner`` explicitly comments
    on — the design decision that DB failure is intentionally indistinguishable
    from a genuine empty-context tick: both lead to suppression, with
    per-tier ``logger.warning`` calls in ``_inject_memory_context`` as the
    operator-visible signal.  This test pins that contract end-to-end so a
    future refactor that, e.g., re-raises tier exceptions instead of
    swallowing them would fail loudly here.
    """

    @pytest.mark.asyncio
    async def test_all_tier_lookups_raising_suppresses_tick(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Episodic + notes recall both raise → DO_NOTHING, no LLM call, warnings logged.

        TICK events have no ``sender_id`` so ``_inject_memory_context`` never
        invokes the relationship tier (see the ``if sender_id:`` guard in
        ``memory_context.py``).  Patching the two tiers that *are* exercised
        on a TICK is sufficient to drive ``memory_admitted_tokens`` to zero
        through the swallowing path.
        """
        client = make_client()
        agent = await make_agent(client=client)

        with (
            patch.object(
                agent._episodic_memory,
                "recall",
                side_effect=OSError("DB unavailable"),
            ),
            patch.object(
                agent._episodic_memory,
                "recall_notes",
                side_effect=OSError("DB unavailable"),
            ),
            caplog.at_level(
                logging.WARNING,
                logger="agents.persona_runtime.memory_context",
            ),
        ):
            actions = await agent.on_event(AgentEvent(event_type=EventType.TICK))

        # Suppression: all exercised tiers raised → admitted=0 → DO_NOTHING.
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.DO_NOTHING
        client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]

        # Operator-visible signal: each failed tier emitted a warning.
        # Use ``getMessage()`` rather than ``.message`` to resolve the
        # ``%s`` placeholders in the format string.
        failure_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "failed, skipping" in r.getMessage()
        ]
        assert len(failure_warnings) >= 2, (
            f"Expected at least 2 tier-failure warnings (episodic + notes), "
            f"got {len(failure_warnings)}: "
            f"{[r.getMessage() for r in failure_warnings]}"
        )
