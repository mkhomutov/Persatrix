"""EXP-001 harness — arms B and C: one meeting in a channel, and its memo turn (PR 5a).

The operator posts the meeting's message word for word, and the advisers
discuss it until the discussion closes, however it closes. At every meeting
but the briefing the memo turn follows: the harness disarms the channel,
makes the other three advisers observers, and asks the chair alone for the
memo, which is the chair's first message after that request (check 5). A
discussion that never closes, and a chair who never answers, are failures
the system causes: recorded, not retried, and the meeting kept as it stands.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from evaluators.exp001.channel_arm import (
    CLOSE_MISMATCH,
    LIMITS,
    MEMO_TURN_WENT_ON,
    MISSING_MEMO,
    NEVER_CLOSED,
    OPERATOR_MESSAGE_LIMIT,
    channel_name,
    hold_meeting,
)
from evaluators.exp001.deployment import REPO, channel_config
from evaluators.exp001.materials import Meeting, MeetingKind, load_series
from evaluators.exp001.orchestrator import Close, Message, OrchestratorLog
from evaluators.exp001.panel import load_panel
from evaluators.exp001.runtime import MemoTurn

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
SERIES = load_series(_EXP / "series-1.yaml")
_T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)
_CHANNEL = "group:advice-2"


def _first(kind: MeetingKind) -> Meeting:
    return next(m for m in SERIES.meetings if m.kind is kind)


class _Room:
    """A channel that plays out a script as a fake clock runs, with the
    orchestrator's log beside it. Offsets are seconds after the post that
    sets them off: the operator's message, then the memo request."""

    def __init__(
        self,
        *,
        replies: Sequence[tuple[float, str]] = ((10, "velvet-pika"), (20, "ripple-kite")),
        close_after: float | None = 35,
        trigger: str = "structural",
        depth_capped: bool = False,
        busy_until: float = 0,
        memo_after: float | None = 12,
        late_synthesis_after: float | None = None,
        stamp: str | None = None,
        chatter_every: float | None = None,
        after_memo: Sequence[tuple[float, str]] = (),
    ) -> None:
        self.seconds = 0.0
        self.actions: list[tuple[Any, ...]] = []
        self.disarmed_at: float | None = None
        self._scheduled: list[Message] = []
        self._closes: list[Close] = []
        self._replies = replies
        self._close_after = close_after
        self._trigger = trigger
        self._depth_capped = depth_capped
        self._busy_until = busy_until  # someone has a turn in flight until then
        self._memo_after = memo_after
        self._late_synthesis_after = late_synthesis_after
        self._stamp = stamp  # the close the memo request is stamped with, if not the real one
        self._chatter_every = chatter_every
        self._after_memo = after_memo
        self._ids = 0

    def _next_id(self) -> str:
        self._ids += 1
        return f"m-{self._ids}"

    def now(self) -> dt.datetime:
        return _T0 + dt.timedelta(seconds=self.seconds)

    async def sleep(self, seconds: float) -> None:
        self.seconds += seconds

    def _say(
        self, offset: float, sender: str, text: str = "a point", **metadata: Any,
    ) -> None:
        mid = self._next_id()
        self._scheduled.append(Message(
            id=mid, sender=sender, content=f"{text} {mid}",
            at=self.now() + dt.timedelta(seconds=offset), metadata=metadata,
        ))

    async def post(
        self, channel: str, sender: str, content: str, *, mentions: Sequence[str] = (),
    ) -> Message:
        self.actions.append(("post", channel, sender, content, tuple(mentions)))
        metadata: dict[str, Any] = {}
        if len(self.actions) > 1:  # the memo request: stamped with the close before it
            trigger = self._stamp or self._closed_by()
            if trigger is not None:
                metadata = {
                    "previous_interaction_id": "i-1", "previous_interaction_close_trigger": trigger,
                }
        message = Message(
            id=self._next_id(), sender=sender, content=content, at=self.now(),
            mentions=tuple(mentions), metadata=metadata,
        )
        self._scheduled.append(message)
        if len(self.actions) == 1:
            self._open()
        else:
            if self._late_synthesis_after is not None:  # the closing synthesis, running late
                self._say(
                    self._late_synthesis_after, "lunar-stoat", "In summary", synthesis_reply=True,
                )
            if self._memo_after is not None:
                self._say(self._memo_after, "lunar-stoat", "## Recommendation")
            for offset, sender_after in self._after_memo:
                self._say(offset, sender_after)
        return message

    def _open(self) -> None:
        for offset, sender in self._replies:
            self._say(offset, sender)
        if self._chatter_every is not None:  # the three who are not the chair, in turn
            for k in range(1, 300):
                self._say(k * self._chatter_every, PANEL.advisers[1 + k % 3].id)
        if self._close_after is not None:
            at = self.now() + dt.timedelta(seconds=self._close_after)
            self._closes.append(Close(_CHANNEL, "i-1", self._trigger, at))

    def _closed_by(self) -> str | None:
        logged = [c for c in self._closes if c.at <= self.now()]
        if logged:
            return logged[0].trigger
        heard = [m.at for m in self._scheduled if m.at <= self.now()]
        return "idle" if self.now() - max(heard) >= LIMITS.idle else None

    async def messages(self, channel: str) -> list[Message]:
        return sorted((m for m in self._scheduled if m.at <= self.now()), key=lambda m: m.at)

    async def activity(self, channel: str) -> set[str]:
        return {"crimson-crow"} if self.seconds < self._busy_until else set()

    async def disarm(self, channel: str) -> None:
        self.actions.append(("disarm", channel))
        self.disarmed_at = self.seconds

    async def set_respond(self, channel: str, member: str, respond: str) -> None:
        """As in the orchestrator, a turn already dispatched still lands."""
        self.actions.append(("set_respond", channel, member, respond))

    def log(self) -> OrchestratorLog:
        closes = tuple(c for c in self._closes if c.at <= self.now())
        return OrchestratorLog(
            closes=closes,
            depth_capped=frozenset({"i-1"} if self._depth_capped else ()),
            spending_limit_refusals=(), refused_leases=(), rate_limit_off=True,
            wallet_served=True,
        )


async def _hold(room: _Room, meeting: Meeting, arm: str = "C", **kwargs: Any) -> Any:
    return await hold_meeting(
        room, room.log, PANEL, arm, SERIES, meeting,
        channel=_CHANNEL, attempt=1, now=room.now, sleep=room.sleep, **kwargs,
    )


class TestAPlanMeeting:
    async def test_the_operator_posts_the_meetings_message_word_for_word(self) -> None:
        room = _Room()
        plan = _first(MeetingKind.PLAN)
        await _hold(room, plan)
        assert room.actions[0] == ("post", _CHANNEL, "operator", plan.message, ())

    async def test_the_memo_turn_follows_the_close(self) -> None:
        room = _Room()
        plan = _first(MeetingKind.PLAN)
        result = await _hold(room, plan)
        assert room.actions[1:] == [
            ("disarm", _CHANNEL),
            ("set_respond", _CHANNEL, "velvet-pika", "observer"),
            ("set_respond", _CHANNEL, "ripple-kite", "observer"),
            ("set_respond", _CHANNEL, "crimson-crow", "observer"),
            ("post", _CHANNEL, "operator", PANEL.memo_turn_instruction(MeetingKind.PLAN),
             ("lunar-stoat",)),
        ]
        closed_at = _T0 + dt.timedelta(seconds=35)
        assert (result.closed_at, result.trigger, result.failures) == (
            closed_at, "structural", (),
        )
        request = result.transcript[3]
        assert result.memo_turn == MemoTurn(
            arm="C", series="series-1", meeting=plan.id, attempt=1, chair="lunar-stoat",
            asked_at=request.at,
        )
        assert result.memo == result.transcript[4]
        assert (result.memo.sender, result.memo.at) == (
            "lunar-stoat", request.at + dt.timedelta(seconds=12),
        )

    async def test_the_memo_turn_waits_for_the_logged_close(self) -> None:
        """Disarming while the chair's closing synthesis is pending abandons
        the close, so quiet alone is not a close until the idle window ends."""
        room = _Room(close_after=300)
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert result.closed_at == _T0 + dt.timedelta(seconds=300)
        assert room.seconds >= 300

    async def test_a_channel_quiet_for_the_idle_window_has_closed(self) -> None:
        room = _Room(close_after=None)
        result = await _hold(room, _first(MeetingKind.PLAN))
        last = _T0 + dt.timedelta(seconds=20)
        assert (result.closed_at, result.trigger, result.failures) == (
            last + LIMITS.idle, "idle", (),
        )
        # The memo turn follows within one poll of the window's end.
        assert result.memo_turn is not None
        late = result.memo_turn.asked_at - (last + LIMITS.idle)
        assert dt.timedelta(0) <= late <= dt.timedelta(seconds=LIMITS.poll_seconds)
        assert result.transcript[-2].closed_before == ("i-1", "idle")

    async def test_the_transcript_holds_every_message_oldest_first(self) -> None:
        result = await _hold(_Room(), _first(MeetingKind.PLAN))
        assert [m.sender for m in result.transcript] == [
            "operator", "velvet-pika", "ripple-kite", "operator", "lunar-stoat",
        ]
        assert result.opened == result.transcript[0]

    async def test_a_recall_check_asks_for_the_answers(self) -> None:
        room = _Room()
        await _hold(room, _first(MeetingKind.RECALL))
        assert room.actions[-1][3] == PANEL.memo_turn_instruction(MeetingKind.RECALL)

    @pytest.mark.parametrize(("room", "closed_by"), [
        (_Room(trigger="end_votes"), "vote"),
        (_Room(trigger="structural", depth_capped=True), "depth_cap"),
        (_Room(trigger="structural"), "round_limit"),
        (_Room(trigger="cost"), "cost"),
        (_Room(close_after=None), "idle"),
    ])
    async def test_what_closed_the_discussion(self, room: _Room, closed_by: str) -> None:
        """Part 2 reports each meeting's close as a vote, the depth cap or the
        round limit; the last two log the same trigger."""
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert result.closed_by == closed_by

    async def test_each_advisers_energy_is_read_as_the_memo_is_asked_for(self) -> None:
        room = _Room()
        read_after: list[int] = []

        def energy() -> dict[str, float | None]:
            read_after.append(len(room.actions))
            return {a.id: 0.8 for a in PANEL.advisers}

        result = await _hold(room, _first(MeetingKind.PLAN), read_energy=energy)
        assert result.energy == {a.id: 0.8 for a in PANEL.advisers}
        assert read_after == [5]  # the message, the disarm, three observers; then the request

    async def test_the_memo_turn_waits_for_turns_still_in_flight(self) -> None:
        """A turn the floor dispatched before the close still posts; it lands
        before the memo request, while the channel is still armed."""
        room = _Room(
            replies=((10, "velvet-pika"), (20, "ripple-kite"), (44, "crimson-crow")),
            busy_until=45,
        )
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert room.disarmed_at is not None and room.disarmed_at >= 45
        assert result.failures == ()

    async def test_a_turn_that_never_ends_holds_the_memo_turn_back_only_so_long(self) -> None:
        room = _Room(busy_until=10_000)
        await _hold(room, _first(MeetingKind.PLAN))
        assert room.disarmed_at is not None
        waited = dt.timedelta(seconds=room.disarmed_at - 35)
        assert LIMITS.drain <= waited <= LIMITS.drain + dt.timedelta(seconds=LIMITS.poll_seconds)

    async def test_a_late_closing_synthesis_is_not_the_memo(self) -> None:
        room = _Room(late_synthesis_after=3, memo_after=12)
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert result.memo is not None and result.memo.content.startswith("## Recommendation")
        assert result.failures == ()

    async def test_an_idle_gap_ended_between_two_polls_is_still_the_close(self) -> None:
        """The orchestrator closes an idle discussion when the next message
        arrives, whenever the harness last looked."""
        room = _Room(replies=((12, "velvet-pika"), (614, "ripple-kite")), close_after=900)
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert (result.closed_at, result.trigger) == (
            _T0 + dt.timedelta(seconds=612), "idle",
        )


class TestABriefing:
    async def test_it_ends_when_its_discussion_closes(self) -> None:
        room = _Room()
        result = await _hold(room, _first(MeetingKind.BRIEFING), arm="B")
        assert [a[0] for a in room.actions] == ["post"]
        assert (
            result.memo_turn, result.memo, result.trigger, result.energy, result.failures,
        ) == (None, None, "structural", {}, ())


class TestFailuresTheSystemCauses:
    async def test_a_discussion_that_never_closes_is_recorded_and_has_no_memo_turn(
        self,
    ) -> None:
        """The memo turn follows a close; asking during a live discussion would
        take the chair's next discussion turn for the memo."""
        room = _Room(close_after=None, chatter_every=30)
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert (result.closed_at, result.trigger, result.closed_by) == (None, None, None)
        assert result.failures == (NEVER_CLOSED,)
        assert room.seconds >= LIMITS.discussion.total_seconds()
        assert [a[0] for a in room.actions] == ["post"]
        assert (result.memo_turn, result.memo) == (None, None)

    async def test_a_close_the_orchestrator_stamped_otherwise_is_recorded(self) -> None:
        """The memo request carries the orchestrator's own record of the close
        before it; a different one means the harness misread the close."""
        room = _Room(trigger="structural", stamp="end_votes")
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert result.failures == (CLOSE_MISMATCH,)

    async def test_a_chair_who_never_answers_leaves_the_memo_missing(self) -> None:
        room = _Room(memo_after=None)
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert (result.memo, result.failures) == (None, (MISSING_MEMO,))
        assert result.memo_turn is not None
        assert room.now() - result.memo_turn.asked_at >= LIMITS.memo

    async def test_the_memo_is_the_chairs_first_message_after_the_request(self) -> None:
        room = _Room(memo_after=12, after_memo=((5, "ripple-kite"), (20, "lunar-stoat")))
        result = await _hold(room, _first(MeetingKind.PLAN))
        assert result.memo is not None and result.memo.content.startswith("## Recommendation")
        assert result.failures == (MEMO_TURN_WENT_ON,)

    async def test_a_message_the_advisers_would_drop_is_refused_unsent(self) -> None:
        room = _Room()
        long = Meeting(
            "x", MeetingKind.PLAN, "Today is Monday 13 October 2036.\n" + "a" * 4000,
            dt.date(2036, 10, 13),
        )
        with pytest.raises(ValueError, match=f"longer than the {OPERATOR_MESSAGE_LIMIT}"):
            await _hold(room, long)
        assert room.actions == []


class TestChannelName:
    def test_each_meeting_of_a_series_has_its_own_channel_the_same_in_every_arm(self) -> None:
        assert [channel_name(SERIES, m) for m in SERIES.meetings] == [
            f"advice-{n}" for n in range(1, 7)
        ]


class TestLimits:
    def test_the_harness_waits_as_long_as_the_frozen_choices_say(self) -> None:
        assert (LIMITS.discussion, LIMITS.idle, LIMITS.drain, LIMITS.memo, LIMITS.settle) == (
            dt.timedelta(minutes=60), dt.timedelta(seconds=600), dt.timedelta(minutes=5),
            dt.timedelta(minutes=35), dt.timedelta(seconds=10),
        )

    def test_the_idle_window_is_the_orchestrators_own(self) -> None:
        """No channel here sets an idle window, so the orchestrator closes an
        idle discussion after its default, the one the harness waits for."""
        fleet = yaml.safe_load((REPO / "config" / "channels.yaml").read_text())
        assert "default_interaction_idle_timeout_seconds" not in fleet
        entry = channel_config(PANEL, "C", name="advice-1", organisation="Linden Loaf")
        assert "interaction_idle_timeout_seconds" not in entry
        go = (REPO / "internal" / "channels" / "config.go").read_text()
        assert "const DefaultInteractionIdleTimeoutSeconds = 600\n" in go
        assert LIMITS.idle == dt.timedelta(seconds=600)
