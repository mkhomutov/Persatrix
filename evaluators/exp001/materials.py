"""Reads the EXP-001 series files and refuses any the run could not use.

Each series is one fictional organisation: a briefing, some plan meetings,
then a recall check, one week apart in the story calendar. The panel only
ever sees a meeting's ``message``; the facts and answer keys are for the
harness and the scorers. The run reads each meeting's story date from the
message's first line ("Today is Monday 6 October 2036.") to set every
adviser's clock to that day.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SCORED_SERIES = tuple(f"series-{n}" for n in range(1, 6))
PRACTICE_SERIES = "practice"
STORY_START = dt.date(2036, 10, 6)
PLANS_PER_SCORED_SERIES = 4
OPTION_LETTERS = frozenset("ABC")

_DATE_LINE = re.compile(r"^Today is (\w+) (\d{1,2} \w+ \d{4})\.$")


class MaterialsError(ValueError):
    """A materials file does not have the shape the pre-registration fixes."""


class MeetingKind(enum.Enum):
    BRIEFING = "briefing"
    PLAN = "plan"
    CONTROL = "control"
    RECALL = "recall"

    @property
    def scored(self) -> bool:
        return self in (MeetingKind.PLAN, MeetingKind.CONTROL)


@dataclass(frozen=True)
class FactUse:
    """How an earlier fact bears on a plan, and what a memo must say to use it."""

    implication: str
    must_state: tuple[str, ...]


@dataclass(frozen=True)
class PlanKey:
    problems: dict[str, str]
    earlier_facts: dict[str, FactUse]
    unsound_options: tuple[str, ...]


@dataclass(frozen=True)
class RecallItem:
    fact: str
    answer: str
    must_include: tuple[str, ...]


@dataclass(frozen=True)
class Meeting:
    id: str
    kind: MeetingKind
    message: str
    story_date: dt.date
    plan_key: PlanKey | None = None
    recall_key: dict[str, RecallItem] | None = None


@dataclass(frozen=True)
class Series:
    id: str
    organisation: str
    facts: dict[str, str]
    meetings: tuple[Meeting, ...]

    @property
    def scored_meetings(self) -> tuple[Meeting, ...]:
        return tuple(m for m in self.meetings if m.kind.scored)


@dataclass(frozen=True)
class Materials:
    series: tuple[Series, ...]
    practice: Series


def load_materials(directory: Path) -> Materials:
    """Load the practice series and the five scored series, in order."""
    series = []
    for sid in SCORED_SERIES:
        path = directory / f"{sid}.yaml"
        if not path.is_file():
            raise MaterialsError(f"{sid}: file {path.name} is missing")
        loaded = load_series(path)
        if loaded.id != sid:
            raise MaterialsError(f"{path.name}: id is {loaded.id!r}, expected {sid!r}")
        if len(loaded.scored_meetings) != PLANS_PER_SCORED_SERIES:
            raise MaterialsError(
                f"{sid}: has {len(loaded.scored_meetings)} plan meetings, expected 4"
            )
        series.append(loaded)
    return Materials(series=tuple(series), practice=load_series(directory / "practice.yaml"))


def load_series(path: Path) -> Series:
    doc = yaml.safe_load(path.read_text())
    sid = str(doc["id"])
    facts = {str(k): str(v) for k, v in doc["facts"].items()}
    _check_kinds(sid, [raw.get("kind") for raw in doc["meetings"]])
    meetings = tuple(_meeting(raw, facts) for raw in doc["meetings"])
    _check_calendar(sid, meetings)
    return Series(id=sid, organisation=str(doc["organisation"]), facts=facts, meetings=meetings)


def _meeting(raw: dict[str, Any], facts: dict[str, str]) -> Meeting:
    mid = str(raw["id"])
    kind = MeetingKind(raw["kind"])
    message = str(raw["message"])
    story_date = _story_date(mid, message)
    key = raw.get("key")
    if (kind.scored or kind is MeetingKind.RECALL) and not isinstance(key, dict):
        raise MaterialsError(f"{mid}: a {kind.value} meeting needs a key")
    if kind.scored:
        return Meeting(mid, kind, message, story_date, plan_key=_plan_key(mid, kind, key, facts))
    if kind is MeetingKind.RECALL:
        return Meeting(mid, kind, message, story_date, recall_key=_recall_key(mid, key, facts))
    return Meeting(mid, kind, message, story_date)


def _story_date(mid: str, message: str) -> dt.date:
    match = _DATE_LINE.match(message.splitlines()[0] if message else "")
    if match is None:
        raise MaterialsError(f"{mid}: the message must start with 'Today is <weekday> <date>.'")
    weekday, text = match.groups()
    try:
        date = dt.datetime.strptime(text, "%d %B %Y").date()
    except ValueError:
        raise MaterialsError(f"{mid}: {text!r} is not a real date") from None
    if date.strftime("%A") != weekday:
        raise MaterialsError(f"{mid}: {text} is a {date.strftime('%A')}, not the weekday {weekday}")
    return date


def _plan_key(mid: str, kind: MeetingKind, raw: Any, facts: dict[str, str]) -> PlanKey:
    uses: dict[str, FactUse] = {}
    for fid, use in (raw.get("earlier_facts") or {}).items():
        if fid not in facts:
            raise MaterialsError(f"{mid}: key names {fid}, which the briefing never gave")
        must_state = tuple(str(s) for s in use.get("must_state") or ())
        if not must_state:
            raise MaterialsError(f"{mid}: {fid} has no must_state details")
        uses[str(fid)] = FactUse(implication=str(use["implication"]), must_state=must_state)
    unsound = tuple(str(u) for u in raw.get("unsound_options") or ())
    if not set(unsound) <= OPTION_LETTERS:
        raise MaterialsError(f"{mid}: unsound options {unsound} are not all among A, B, C")
    if kind is MeetingKind.CONTROL and (uses or unsound):
        raise MaterialsError(f"{mid}: a control plan lists no earlier facts and no unsound options")
    if kind is MeetingKind.PLAN and not uses:
        raise MaterialsError(f"{mid}: a plan meeting must list the earlier facts that bear on it")
    if kind is MeetingKind.PLAN and not unsound:
        raise MaterialsError(f"{mid}: a plan meeting must name the option its facts make unsound")
    problems = {str(k): str(v) for k, v in raw["problems"].items()}
    return PlanKey(problems=problems, earlier_facts=uses, unsound_options=unsound)


def _recall_key(mid: str, raw: Any, facts: dict[str, str]) -> dict[str, RecallItem]:
    items = {}
    for qid, item in raw.items():
        if item["fact"] not in facts:
            raise MaterialsError(
                f"{mid}: {qid} tests {item['fact']}, which the briefing never gave"
            )
        items[str(qid)] = RecallItem(
            fact=str(item["fact"]),
            answer=str(item["answer"]),
            must_include=tuple(str(s) for s in item["must_include"]),
        )
    return items


def _check_kinds(sid: str, raw_kinds: list[Any]) -> None:
    """Briefing first, recall last, plans between — checked before any key is read."""
    try:
        kinds = [MeetingKind(k) for k in raw_kinds]
    except ValueError as exc:
        raise MaterialsError(f"{sid}: unknown meeting kind ({exc})") from None
    if not kinds or kinds[0] is not MeetingKind.BRIEFING:
        raise MaterialsError(f"{sid}: the first meeting must be the briefing")
    if kinds[-1] is not MeetingKind.RECALL:
        raise MaterialsError(f"{sid}: the last meeting must be the recall check")
    if not all(k.scored for k in kinds[1:-1]) or len(kinds) < 3:
        raise MaterialsError(
            f"{sid}: every meeting between briefing and recall must be a plan or control kind"
        )


def _check_calendar(sid: str, meetings: tuple[Meeting, ...]) -> None:
    ids = [m.id for m in meetings]
    if len(set(ids)) != len(ids):
        raise MaterialsError(f"{sid}: duplicate meeting id")
    if meetings[0].story_date != STORY_START:
        raise MaterialsError(f"{sid}: the briefing must fall on Monday 6 October 2036")
    for before, after in zip(meetings, meetings[1:], strict=False):
        if after.story_date - before.story_date != dt.timedelta(days=7):
            raise MaterialsError(f"{after.id}: must be one week after {before.id}")
