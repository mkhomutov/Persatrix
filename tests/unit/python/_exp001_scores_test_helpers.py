"""Shared pieces of the EXP-001 scoring's tests: a judge that scores each
arm's answers by a table, the scored and practice runs it judges, and the
two people's score sheets filled from the same table.

Every test answer names its arm, series and meeting ("C's chair at
series-1-plan-1."), so a stand-in rater can score by arm without the seal.
That is what lets these tests make up scores that fire a known rule; no
real packet carries its arm.

Extracted so ``test_exp001_scoring_run.py`` (opening the seal) and
``test_exp001_result.py`` (the figures) share them. The pattern mirrors
``_exp001_scored_test_helpers.py``: a private module beside the test files,
imported by name.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agents.llm_client import LLMClient, LLMResponse, Usage
from evaluators.exp001.attempts import Held
from evaluators.exp001.judge import packet_kind
from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.practice import run_practice
from evaluators.exp001.rating import packet_text, read_packets
from evaluators.exp001.sheets import sheet_path

from ._exp001_judge_test_helpers import PROMPTS
from ._exp001_run_test_helpers import PANEL, SCORED, SERIES, channel_meeting, no_wait
from ._exp001_scored_test_helpers import _BINARY, NAMES, _Holds, _Judge, _scored

__all__ = [
    "LONG_MEMO",
    "MISSING_MEMO",
    "TABLE",
    "ArmJudge",
    "Scorer",
    "build_practice",
    "build_scored",
    "fill_sheets",
    "reversed_scorer",
    "table_scorer",
]

# Each arm's C1..C5 on every memo, but A's C5 in series-3, which is 0.
TABLE = {
    "A": (1, 1, 1, 1, 1),
    "B": (1, 1, 2, 1, 1),
    "C": (2, 1, 2, 2, 1),
    "D": (2, 2, 2, 2, 2),
    "D-prime": (2, 1, 2, 2, 2),
}
_RIGHT = frozenset({"D", "D-prime"})  # the arms whose recall answers are right
_ANSWER = re.compile(r"^(A|B|C|D|D-prime)(?:'s chair)? at ([\w-]+?)-(plan-\d+|recall)\.",
                     re.MULTILINE)

# B's first plan of series-2 is never written, and D′'s last plan of series-1
# runs past 400 words.
MISSING_MEMO = ("B", SCORED[1].id, SCORED[1].scored_meetings[0].id)
LONG_MEMO = ("D-prime", SCORED[0].id, SCORED[0].scored_meetings[-1].id)

# Scores a packet's answer: from its arm, series, and whether the key lists
# no facts (a control plan), a memo's five scores; from its arm and the
# recall key's questions, each marked right or wrong.
Scorer = Callable[[str, str, bool], dict[str, int | None]]


def table_scorer(arm: str, series: str, control: bool) -> dict[str, int | None]:
    scores: dict[str, int | None] = dict(zip(("C1", "C2", "C3", "C4", "C5"), TABLE[arm],
                                             strict=True))
    if arm == "A" and series == "series-3":
        scores["C5"] = 0
    if control:
        scores["C2"] = None
    return scores


def reversed_scorer(arm: str, series: str, control: bool) -> dict[str, int | None]:
    """The table read upside down: A scores as D does, B as D′, and the reverse."""
    mirror = {"A": "D", "B": "D-prime", "C": "C", "D": "A", "D-prime": "B"}
    return table_scorer(mirror[arm], series, control)


def _answer(text: str) -> tuple[str, str]:
    """The arm and series an answer in a test packet names."""
    found = _ANSWER.search(text)
    assert found is not None, text
    return found.group(1), found.group(2)


class ArmJudge(_Judge):
    """The judge, scoring each answer by the table; each call costs *output*
    output tokens on the judge's model."""

    def __init__(self, *, output: int = 900) -> None:
        super().__init__()
        self.output = output

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        packet = kwargs["messages"][0]["content"]
        arm, series = _answer(packet)
        if kwargs["system"] == PROMPTS.recall:
            questions = re.findall(r"^(R\d+), question", packet, flags=re.MULTILINE)
            answer = json.dumps(dict.fromkeys(questions, "right" if arm in _RIGHT else "wrong"))
        else:
            scores = table_scorer(arm, series, "decision: none." in packet)
            answer = json.dumps({**scores, "reasons": {}})
        return LLMResponse(text=answer, usage=Usage(3000, self.output))


def _acts() -> dict[Any, Held[Any] | BaseException]:
    acts: dict[Any, Held[Any] | BaseException] = {}
    for (arm, sid, mid), memo in ((MISSING_MEMO, None),
                                  (LONG_MEMO, "{arm}'s chair at {mid}.\n" + "word " * 450)):
        series = next(s for s in SCORED if s.id == sid)
        meeting = next(m for m in series.meetings if m.id == mid)
        written = None if memo is None else memo.format(arm=arm, mid=mid)
        acts[(arm, sid, mid, 1, 1)] = Held(channel_meeting(
            meeting, 1, 1, arm=arm, series=series, memo=written,
        ))
    return acts


async def build_scored(root: Path, *, judge: ArmJudge | None = None) -> dict[str, Any]:
    """A scored run held in *root*, every series kept, judged by the table."""
    return await _scored(root, _Holds(acts=_acts()), judge or ArmJudge())


async def build_practice(root: Path) -> dict[str, Any]:
    """A practice run held in *root*, every arm, judged by the table."""
    holds = _Holds()
    return await run_practice(
        root, ("A", "B", "C", "D", "D-prime"), panel=PANEL, series=SERIES, scored=SCORED,
        names=NAMES, client=LLMClient(ArmJudge()), binary=_BINARY, prompts=PROMPTS,
        sleep=no_wait, make_hold=lambda arm, directory: holds(arm, SERIES, directory),
    )


def fill_sheets(directory: Path, scorers: Mapping[str, Scorer], *, leave: int = 0) -> None:
    """Fill each person's blank sheet in *directory* by their scorer, the
    last *leave* rows of each left blank."""
    drawn = read_packets(directory)
    for person, scorer in scorers.items():
        path = sheet_path(directory, person)
        with path.open(newline="") as sheet:
            header, *rows = list(csv.reader(sheet))
        for row in rows[: len(rows) - leave]:
            packet = drawn.packets[row[1]]
            arm, series = _answer(packet_text(packet))
            if row[2] == "recall":
                for column, cell in enumerate(row):
                    if header[column].startswith("R") and cell != "-":
                        row[column] = "right" if arm in _RIGHT else "wrong"
                continue
            scores = scorer(arm, series, packet_kind(packet) is MeetingKind.CONTROL)
            for criterion, score in scores.items():
                if score is not None:
                    row[header.index(criterion)] = str(score)
        with path.open("w", newline="") as sheet:
            csv.writer(sheet).writerows([header, *rows])
