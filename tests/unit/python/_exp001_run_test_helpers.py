"""Shared pieces of the EXP-001 run's tests: the practice series and panel,
and an arm's record of a meeting as a hold returns it.

Extracted so ``test_exp001_pairs.py`` (holding one arm's series) and
``test_exp001_practice.py`` (the practice run) build the same records. The
pattern mirrors ``_exp001_judge_test_helpers.py``: a private module beside
the test files, imported by name.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from agents.llm_types import StopReason
from evaluators.exp001.arm_a import ArmAReply
from evaluators.exp001.channel_arm import ChannelMeeting
from evaluators.exp001.materials import Meeting, MeetingKind, load_series
from evaluators.exp001.orchestrator import Message
from evaluators.exp001.panel import load_panel
from evaluators.exp001.runtime import MemoTurn

__all__ = [
    "EXP",
    "IDS",
    "PANEL",
    "SERIES",
    "T0",
    "arm_a_reply",
    "channel_meeting",
    "no_wait",
]

EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(EXP / "panel.yaml")
SERIES = load_series(EXP / "practice.yaml")
IDS = [m.id for m in SERIES.meetings]
T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)


async def no_wait(seconds: float) -> None:
    """Stands in for asyncio.sleep between tries."""


def _message(n: int, sender: str, content: str) -> Message:
    return Message(f"m-{n}", sender, content, T0 + dt.timedelta(minutes=n))


def channel_meeting(
    meeting: Meeting, attempt: int, meeting_try: int, *, arm: str = "C",
    memo: str | None = "The memo.", failures: tuple[str, ...] = (),
) -> ChannelMeeting:
    """A channel arm's record of *meeting*, with its memo turn unless it is the briefing."""
    opened = _message(0, PANEL.operator, meeting.message)
    briefing = meeting.kind is MeetingKind.BRIEFING
    turn = None if briefing else MemoTurn(
        arm=arm, series=SERIES.id, meeting=meeting.id, attempt=attempt,
        meeting_try=meeting_try, chair=PANEL.chair.id, asked_at=T0 + dt.timedelta(minutes=5),
    )
    written = None if briefing or memo is None else _message(6, PANEL.chair.id, memo)
    return ChannelMeeting(
        arm=arm, series=SERIES.id, meeting=meeting.id, attempt=attempt, meeting_try=meeting_try,
        channel=f"group:advice-{IDS.index(meeting.id) + 1}", opened=opened,
        closed_at=T0 + dt.timedelta(minutes=4), trigger="end_votes", closed_by="vote",
        memo_turn=turn, energy={PANEL.chair.id: 0.8}, memo=written,
        transcript=(opened, *(() if written is None else (written,))), failures=failures,
    )


def arm_a_reply(meeting: Meeting, text: str, stop: StopReason = StopReason.END_TURN) -> ArmAReply:
    """Arm A's reply at *meeting*."""
    return ArmAReply(
        series=SERIES.id, meeting=meeting.id, text=text, stop_reason=stop,
        asked_at=T0, answered_at=T0 + dt.timedelta(seconds=20),
    )
