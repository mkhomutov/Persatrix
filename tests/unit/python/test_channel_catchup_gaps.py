"""ISSUE-0130 (b) — what an INCOMPLETE catch-up pass reports.

Third file in the ``channel_catchup`` family (after ``test_channel_catchup``
and ``test_channel_catchup_followups``), split at the 500-line cap
``scripts/checks/file_size.py --strict`` enforces.  Its subject is narrow
and worth isolating: the two ways a pass can fail to deliver a whole
window, and why they do not have the same blast radius.

A budget overrun stops the loop, so every record that channel opened holds
a prefix.  A row that RAISES leaves a hole in exactly one speaker's record,
because records are keyed ``(principal, speaker, scope)`` — and since such
a failure is deterministic (the ``except`` calls reaching it "a programming
error somewhere upstream"), disqualifying the whole channel for it meant
that room's replayed memory was never derived for ANY speaker, on any
boot.
"""

from __future__ import annotations

import aiohttp

from agents.channel_catchup import replay_channel_history
from agents.channel_replay_outcome import ReplayPassOutcome

from ._catchup_test_helpers import _channel, _msg, _SpyAgent


class TestARaisingRowGapsOneSpeakerNotTheRoom:
    """ISSUE-0130 (b), PR B2 review round 3.

    A row whose ``on_event`` RAISES leaves a hole in the record its own
    sender's turns land in — records are keyed `(principal, speaker,
    scope)`, so it cannot gap anyone else's. Withholding the whole channel
    for it was wrong in a way that never recovered: the ``except`` calls
    reaching it "a programming error somewhere upstream", i.e.
    deterministic, so the same row raised on every boot and that room's
    replayed memory was never derived for ANY speaker — indistinguishable,
    from the outside, from replay having stopped working.
    """

    async def test_the_channel_still_completes_and_only_one_speaker_gaps(
        self, orchestrator,
    ):
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m-2", channel_id="group:planning",
                 sender_id="bob", content="fine here"),
            _msg(msg_id="m-1", channel_id="group:planning",
                 sender_id="alice", content="boom"),
        ]

        class _RaisesForAlice(_SpyAgent):
            async def on_event(self, event):
                if event.sender_id == "alice":
                    raise RuntimeError("upstream programming error")
                return await super().on_event(event)

        agent = _RaisesForAlice("ember-owl")
        outcome = ReplayPassOutcome()
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url,
                session=session, outcome=outcome,
            )

        assert outcome.completed == {"group:planning"}, (
            "the loop reached the end of the window, so bob's record holds "
            "his whole side of it and must still be derivable"
        )
        assert outcome.speaker_gaps == {("group:planning", "alice")}, (
            "the hole is alice's alone"
        )

    async def test_an_unnameable_sender_still_takes_the_channel_down(
        self, orchestrator,
    ):
        """The conservative fallback, and the only one available.

        Without a readable ``sender_id`` the gap could be in any record, so
        the channel leaves ``completed`` rather than guessing.
        """
        base_url, state = orchestrator
        state["channels"] = [_channel(channel_id="group:planning")]
        state["members"]["group:planning"] = [
            {"id": "ember-owl", "respond": "when_mentioned",
             "joined_at": "2026-05-01T00:00:00+00:00"},
        ]
        state["history"]["group:planning"] = [
            _msg(msg_id="m-1", channel_id="group:planning",
                 sender_id="alice", content="boom"),
        ]

        class _RaisesAndForgetsSender(_SpyAgent):
            async def on_event(self, event):
                event.sender_id = None
                raise RuntimeError("upstream programming error")

        agent = _RaisesAndForgetsSender("ember-owl")
        outcome = ReplayPassOutcome()
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url,
                session=session, outcome=outcome,
            )
        # The row still names its sender on the WIRE, which is what the
        # loop reads — so this pins the wire-side fallback instead.
        assert outcome.speaker_gaps == {("group:planning", "alice")}
        assert outcome.completed == {"group:planning"}


class TestAPassStopsAtTheFirstRateLimit:
    """ISSUE-0187 step 1.

    The agents' REST calls carry the hosted agent's ID, so a call the
    orchestrator refuses for going over that agent's budget counts toward
    its quarantine, and five in ten minutes quarantine it. A pass that
    carried on past a 429 would collect one refusal per remaining channel;
    it stops instead, so a pass adds at most one.
    """

    async def test_one_refusal_then_the_pass_stops(self, orchestrator):
        base_url, state = orchestrator
        member = [{"id": "ember-owl", "respond": "when_mentioned",
                   "joined_at": "2026-05-01T00:00:00+00:00"}]
        for i in range(10):
            cid = f"group:room-{i}"
            state["channels"].append(_channel(channel_id=cid))
            state["members"][cid] = member
            state["history"][cid] = [
                _msg(msg_id=f"m-{i}", channel_id=cid,
                     sender_id="alice", content="hello"),
            ]
        # The list, then room-0's membership and history, then room-1's
        # membership; room-1's history is the first call refused.
        state["budget"] = 4

        agent = _SpyAgent("ember-owl")
        outcome = ReplayPassOutcome()
        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=agent, orchestrator_url=base_url,
                session=session, outcome=outcome,
            )

        assert state["limited"] == 1, "the pass stops at the first 429"
        assert len(state["log"]) == 5
        assert outcome.completed == {"group:room-0"}, (
            "room-1's window never arrived, so it is not complete"
        )
        assert [e.channel_id for e in agent.events] == ["group:room-0"]

    async def test_a_refused_membership_read_stops_the_pass_too(
        self, orchestrator,
    ):
        base_url, state = orchestrator
        for i in range(10):
            state["channels"].append(_channel(channel_id=f"group:room-{i}"))
        state["budget"] = 1  # the list only

        async with aiohttp.ClientSession() as session:
            await replay_channel_history(
                agent=_SpyAgent("ember-owl"), orchestrator_url=base_url,
                session=session,
            )

        assert state["limited"] == 1
