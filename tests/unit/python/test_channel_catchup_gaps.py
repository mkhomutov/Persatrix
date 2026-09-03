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
