"""RFC 0052 PR 4b-ii — OQ #6: the autonomous-close summary draws a lease.

TDD-first, pinning the ``summarize_close.py`` metering edit
(``docs/rfcs/0052-pr-plan.md``): the RFC 0020 close summary was verified
UNMETERED at OQ #6 resolution — ``summarize_closed_interaction`` passes no
``cause`` / ``interaction_id`` to :meth:`LLMClient.create_message`, so the
call bypasses the wallet lease entirely. On the AUTONOMOUS bounded close
(the interaction marked by the ``close_notification_close_trigger`` wire
field — ``test_close_notification_redelivery.py`` pins the marking) the
summary must now thread:

* ``cause=CAUSE_CHANNEL_MESSAGE`` — arms the RFC 0023 lease bracket;
* ``interaction_id=<the governance wire id>`` — Layer 1 attribution, so
  the summary's spend counts toward the mandatory
  ``interaction_budget_tokens`` cap the PR 4a ``1 + N`` reserve was carved
  from (one such call per participating persona — the ``N``);
* ``agent_id`` — the lease's persona attribution.

The HUMAN close path must stay byte-for-byte unchanged: an unmarked
interaction's summariser call carries none of the lease kwargs (not even
explicit defaults — the call signature itself is the regression surface).
"""

from __future__ import annotations

from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMResponse, StopReason, Usage
from agents.memory.interactions import Interaction, Turn
from agents.persona_runtime.summarize_close import summarize_closed_interaction


class _SpyClient:
    """Records every ``create_message`` kwargs dict verbatim.

    The lease bracket lives inside :meth:`LLMClient.create_message`
    itself, so the metering contract is pinned at that call boundary —
    what matters is exactly which kwargs the summariser passes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_message(self, **kwargs: object) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(
            text="A concise synthesis of the discussion.",
            stop_reason=StopReason.END_TURN,
            usage=Usage(120, 30),
        )


def _interaction(
    *, metered: bool = False, wire_id: str = "wire-ix-1",
) -> Interaction:
    """Two turns so the summariser takes the LLM path (a bodyless
    single-turn interaction short-circuits to the placeholder)."""
    ix = Interaction(
        interaction_id="ix-metering",
        scope="group:planning",
        started_at=0.0,
        closed_at=10.0,
        close_reason="cost",
        wire_interaction_id=wire_id,
        turns=[
            Turn(at=0.0, payload={"sender": "iron-fox", "summary": "hi"}),
            Turn(at=5.0, payload={"sender": "ember-owl", "summary": "hey"}),
        ],
    )
    ix.meter_close_summary = metered
    return ix


class TestAutonomousCloseSummaryIsLeased:
    async def test_metered_interaction_threads_the_lease_attribution(self):
        client = _SpyClient()

        await summarize_closed_interaction(
            client, "ember-owl", _interaction(metered=True),
        )

        assert len(client.calls) == 1
        call = client.calls[0]
        assert call.get("cause") == walletpb.CAUSE_CHANNEL_MESSAGE
        assert call.get("interaction_id") == "wire-ix-1", (
            "the lease bills the GOVERNANCE interaction id — the one the "
            "wallet's per-interaction cap and the router's soft-budget "
            "close trigger both key on"
        )
        assert call.get("agent_id") == "ember-owl"

    async def test_metered_but_untracked_interaction_stays_unleased(self):
        """No governance wire id, nothing to bill: a lease with an empty
        ``interaction_id`` would draw against no cap while changing the
        call's failure mode (a wallet outage would fail it closed), so
        the defensive posture is the unleased status quo. By CP2
        construction a bounded-close notification always carries the
        retired record's wire id, so this is drift defence, not a path."""
        client = _SpyClient()

        await summarize_closed_interaction(
            client, "ember-owl", _interaction(metered=True, wire_id=""),
        )

        call = client.calls[0]
        assert "cause" not in call
        assert "interaction_id" not in call

    async def test_human_close_call_signature_is_unchanged(self):
        """The regression the PR plan demands: the human-channel close is
        byte-for-byte unchanged, so the unmarked summariser call must not
        even carry the lease kwargs as explicit defaults."""
        client = _SpyClient()

        await summarize_closed_interaction(
            client, "ember-owl", _interaction(metered=False),
        )

        assert len(client.calls) == 1
        call = client.calls[0]
        assert "cause" not in call
        assert "interaction_id" not in call
        assert "agent_id" not in call

    async def test_interaction_default_is_unmetered(self):
        """The dataclass default: nothing marks, nothing meters — every
        pre-4b-ii construction site keeps the unleased summary."""
        assert _interaction().meter_close_summary is False
