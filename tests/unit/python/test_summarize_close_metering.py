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

``TestVoteAsSynthesisCloseIsMetered`` (PR #718 review) pins the SECOND
autonomous-close shape OQ #6 must cover: a chair answering the §D synthesis
directive with an END_INTERACTION_VOTE whose content IS the synthesis (the
ISSUE-0099 outcome-(a) shape). Go's fanout-head claims that publish as the
closing artifact BEFORE ``processEndVote``, so the voter's parked local
close (``vote_close.py``) is that bounded close's own record — and the
later close notification, the only other metering writer, no-ops on the
already-closed scope ("invent nothing locally"). Without the discharge-side
mark the chair's close summary ran unleased, silently evading the cap.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from agents.action_executor import ActionExecutor
from agents.channel_wire_metadata import DispatchContext
from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMResponse, StopReason, Usage
from agents.memory.interactions import (
    SUMMARY_UNAVAILABLE_TEXT,
    Interaction,
    InteractionTracker,
    Turn,
)
from agents.persona_runtime.summarize_close import summarize_closed_interaction
from agents.persona_runtime.vote_close import (
    PendingVoteClose,
    discharge_end_vote_publish,
)
from agents.persona_types import ActionType, AgentAction
from agents.wallet_client import BudgetExceededError


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

    async def test_metered_but_untracked_interaction_stays_unleased(self, caplog):
        """No governance wire id, nothing to bill: a lease with an empty
        ``interaction_id`` would draw against no cap while changing the
        call's failure mode (a wallet outage would fail it closed), so
        the defensive posture is the unleased status quo. By CP2
        construction a bounded-close notification always carries the
        retired record's wire id, so this is drift defence, not a path.

        PR #718 review finding 2: the fall-through also WARNS, so an
        unmetered summary from a drifted/compromised producer is
        observable rather than a silent hole once the reserve is enforced."""
        client = _SpyClient()

        with caplog.at_level(
            logging.WARNING, logger="agents.persona_runtime.summarize_close",
        ):
            await summarize_closed_interaction(
                client, "ember-owl", _interaction(metered=True, wire_id=""),
            )

        call = client.calls[0]
        assert "cause" not in call
        assert "interaction_id" not in call
        assert any(
            "no wire interaction id" in r.getMessage() and "UNLEASED" in r.getMessage()
            for r in caplog.records
        ), "the unmetered fall-through warns (finding 2)"

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

    async def test_lease_denied_is_classified_budget_denied(
        self, caplog, monkeypatch,
    ):
        """PR #718 review: a metered summary's wallet denial — the tracked
        lease-side reserve gap (``synthesis_reserve.go`` KNOWN GAPs) biting
        under exactly the cost pressure that fired the close — must land on
        the failure counter as ``budget_denied`` (the established
        ``llm_call_errors`` tick-idle vocabulary), not the generic
        ``llm_error`` that made cap pressure indistinguishable from a
        provider outage. The fallback contract itself is unchanged: the
        unavailable placeholder commits, and the log carries the denial's
        own reason."""

        class _DenyingClient:
            async def create_message(self, **kwargs: object) -> LLMResponse:
                raise BudgetExceededError(
                    "lease denied — interaction cost ceiling exceeded",
                    reason="interaction_budget_exhausted",
                )

        reasons: list[str] = []
        monkeypatch.setattr(
            "agents.persona_runtime.summarize_close._emit_summary_failed",
            reasons.append,
        )
        with caplog.at_level(
            logging.WARNING, logger="agents.persona_runtime.summarize_close",
        ):
            summary, failed, facts, _projections = await summarize_closed_interaction(
                _DenyingClient(), "ember-owl", _interaction(metered=True),
            )

        assert (summary, failed, facts) == (SUMMARY_UNAVAILABLE_TEXT, True, None)
        assert reasons == ["budget_denied"]
        assert any(
            "lease denied" in r.getMessage()
            and "interaction_budget_exhausted" in r.getMessage()
            for r in caplog.records
        ), "the log carries the wallet's own denial reason"


class _VoterAgent:
    """The vote-close seam surface ``discharge_end_vote_publish`` walks
    (the ``_LLMPersonaAgent`` attributes ``vote_close.py`` documents), with
    ``resolve_end_vote_publish`` wired exactly like the real agent's — so
    the tests below drive the WHOLE chain the fix threads: end-vote publish
    → executor outcome callback → discharge → (metered) close → persist."""

    def __init__(self):
        self.agent_id = "quartz-heron"
        self._lock = asyncio.Lock()
        self._interaction_tracker = InteractionTracker(idle_timeout_sec=600.0)
        self._pending_vote_closes = {}
        self._vote_closed_wire_ids = {}
        self.persisted = []

    async def _persist_closed_interaction(self, interaction):
        self.persisted.append(interaction)

    async def resolve_end_vote_publish(
        self, channel_id, *, published, token, synthesis_reply=False,
    ):
        await discharge_end_vote_publish(
            self, channel_id, published=published, token=token,
            synthesis_reply=synthesis_reply,
        )


def _chair_with_open_record() -> tuple[_VoterAgent, ActionExecutor, AsyncMock]:
    """A chair mid-discussion: two ingested turns under the governance wire
    id, the decide-time park stamped for one in-flight vote, and an executor
    whose dispatcher resolves the chair for the outcome callback."""
    agent = _VoterAgent()
    tracker = agent._interaction_tracker
    tracker.add_turn("group:planning", {"sender": "iron-fox", "summary": "hi"})
    record = tracker.add_turn(
        "group:planning", {"sender": "quartz-heron", "summary": "draft"},
    )
    record.wire_interaction_id = "wire-ix-1"
    agent._pending_vote_closes["group:planning"] = PendingVoteClose(
        scope="group:planning", interaction_id=record.interaction_id,
        token="tok-1", in_flight=1,
    )
    publisher = AsyncMock()
    publisher.publish = AsyncMock(return_value=None)
    dispatcher = MagicMock()
    dispatcher.get_agent = MagicMock(return_value=agent)
    executor = ActionExecutor(dispatcher=dispatcher, channel_publisher=publisher)
    return agent, executor, publisher


def _synthesis_vote() -> AgentAction:
    return AgentAction(ActionType.END_INTERACTION_VOTE, {
        "channel_id": "group:planning",
        "content": "Synthesis: adopt option B; revisit tooling next quarter.",
        "vote_close_token": "tok-1",
    })


class TestVoteAsSynthesisCloseIsMetered:
    """PR #718 review, twice — the vote-as-synthesis discharge withholds the
    local close (module docstring): the ``synthesis_reply`` echo says what
    the wire CARRIED, never what Go ACCEPTED, so the metered close belongs
    to the close-notification self-echo (pinned by the redelivery tests)."""

    async def test_synthesis_vote_discharge_defers_to_the_self_echo(self):
        """Chair answers the §D directive with a vote → publish reports
        ``published`` carrying the reply echo → the discharge pops the park
        but closes NOTHING: the record stays open and unmetered for the
        close-notification self-echo (Go's acceptance signal) to close with
        the truthful trigger and the OQ #6 mark. This is also the
        raise-abandon regression pin: when Go refused the claim and left the
        interaction open (a mid-arm ``max_rounds`` raise), the old
        presumptive close buried the chair's live record and billed a lease
        against the extended discussion — from the chair's side the two arcs
        are indistinguishable, which is exactly why the discharge must not
        presume."""
        agent, executor, publisher = _chair_with_open_record()

        results = await executor.execute(
            "quartz-heron", [_synthesis_vote()],
            context=DispatchContext(
                cascade_depth=1,
                origin_channel_id="group:planning",
                origin_interaction_id="wire-ix-1",
                origin_synthesis_turn=True,
            ),
        )

        assert results[0]["status"] == "published"
        assert results[0]["synthesis_reply"] is True
        # The wire claim carried the echo beside the vote flag and id claim.
        assert publisher.publish.await_args.kwargs["metadata"] == {
            "end_interaction_vote": True,
            "interaction_id": "wire-ix-1",
            "synthesis_reply": True,
        }
        # The park is discharged (idempotence: a duplicate callback no-ops)…
        assert agent._pending_vote_closes == {}
        # …but the record is untouched: open, unmetered, nothing persisted —
        # if Go abandoned the arm, the live discussion keeps ingesting here.
        record = agent._interaction_tracker.get("group:planning")
        assert record is not None and record.is_open
        assert record.meter_close_summary is False
        assert agent.persisted == []

    async def test_demoted_synthesis_vote_record_keeps_ingesting(self):
        """The raise-abandon aftermath: with the discharge deferring, a
        continued discussion's next turn lands on the SAME open record —
        no fragmented "ended" local record, no phantom lease."""
        agent, executor, publisher = _chair_with_open_record()

        await executor.execute(
            "quartz-heron", [_synthesis_vote()],
            context=DispatchContext(
                cascade_depth=1,
                origin_channel_id="group:planning",
                origin_interaction_id="wire-ix-1",
                origin_synthesis_turn=True,
            ),
        )
        record = agent._interaction_tracker.get("group:planning")
        continued = agent._interaction_tracker.add_turn(
            "group:planning", {"sender": "iron-fox", "summary": "one more"},
        )
        assert continued is record
        assert record.is_open and record.meter_close_summary is False

    async def test_ordinary_end_vote_close_stays_unleased(self):
        """The negative pin: an end-vote publish with NO synthesis directive
        behind it (an ordinary quorum vote) still closes unmetered, and the
        summariser call keeps the unleased signature byte-for-byte — the
        same regression surface ``test_human_close_call_signature_is_unchanged``
        pins for the human path."""
        agent, executor, publisher = _chair_with_open_record()

        results = await executor.execute(
            "quartz-heron", [_synthesis_vote()],
            context=DispatchContext(
                cascade_depth=1,
                origin_channel_id="group:planning",
                origin_interaction_id="wire-ix-1",
            ),
        )

        assert results[0]["status"] == "published"
        assert results[0]["synthesis_reply"] is False
        closed = agent.persisted[0]
        assert closed.meter_close_summary is False

        client = _SpyClient()
        await summarize_closed_interaction(client, "quartz-heron", closed)
        call = client.calls[0]
        assert "cause" not in call
        assert "interaction_id" not in call
        assert "agent_id" not in call
