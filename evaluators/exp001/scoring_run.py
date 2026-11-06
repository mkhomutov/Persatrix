"""Scoring an EXP-001 run: the people's sheets, the judge's answers, then the seal.

Pre-registration §6 step 4: two people and the LLM judge score every packet
drawn, and the map from packet ID to arm stays sealed until every score is
in (part 2 §2). Scoring a run's directory, a scored run's or a practice
run's, does this:

- **Before every score is in**, it writes each person a blank score sheet
  (:mod:`evaluators.exp001.sheets`) the first time, and says how many of
  their packets each rater has left: each person's from their sheet, the
  judge's from its batch, which it reads without asking anything
  (:func:`evaluators.exp001.judge.read_batch`). It reads no seal, no
  answer's arm and no meeting's record.
- **Once every score is in, and only when asked**, it opens the seal. Once
  the result is worked out, it keeps ``opened.json`` beside the packets: the
  moment, which is when scoring finished, a SHA-256 of each person's scores
  as their sheet gave them, and one of each file the harness wrote that the
  result reads. Opened, the seal stays open, and a score or file that
  changes after it is refused, since no score may change once the arms are
  known.
- **The result.** It reads every score back through the seal and works out
  the figures of part 2 (:mod:`evaluators.exp001.result`): with them, each
  arm's dollars per plan, at the fixed table and repriced, from its calls as
  the scored run's report counts them, its minutes per plan, and for each
  meeting what closed it, each adviser's energy at the memo turn and
  whether the memo was cut at 400 words. It writes them to ``scores.json``
  and in plain words to ``scores.txt``, in the run's directory.

A scored run is scored only once it is complete: its last window is
closed by no fault and holds the packets of four series or more. Scoring is
on time when the seal is opened within 21 days of the last scored meeting's
end. A practice run has no due date, and its one series fires no rule.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluators.exp001 import practice, result_report, scored, scored_report
from evaluators.exp001.attempts import SeriesRun
from evaluators.exp001.costs import (
    ARMS,
    PRICES,
    REPRICING_PRICES,
    dollars_per_plan,
    repriced,
)
from evaluators.exp001.judge import (
    CALL_LOG,
    RATER,
    REPLIES,
    Judged,
    JudgePrompts,
    answered,
    read_batch,
    sole_run,
)
from evaluators.exp001.materials import (
    PLANS_PER_SCORED_SERIES,
    PRACTICE_SERIES,
    Materials,
    MeetingKind,
    Series,
)
from evaluators.exp001.packets import RecallPacket
from evaluators.exp001.pairs import Kept, pair_calls, read_pair, write_json
from evaluators.exp001.rating import (
    PACKETS,
    PEOPLE,
    RATERS,
    SEAL,
    SEALED,
    Drawn,
    Seal,
    read_packets,
    read_seal,
)
from evaluators.exp001.result import Ratings, build
from evaluators.exp001.scored_logs import BATCH, JUDGING, PAIRS, window_directory
from evaluators.exp001.scoring import MemoRef
from evaluators.exp001.sheets import Sheet, read_sheet, sheet_path, write_blank_sheets

OPENED = "opened.json"
RESULT = "scores.json"
RESULT_SUMMARY = "scores.txt"
# Each file the harness wrote that the result reads, as ``opened.json`` names it.
_PINNED = {
    "judge": "the judge's answers",
    "judge_calls": "the judge's calls",
    "packets": "the packets",
    "seal": "the seal",
}


class ScoringRefused(RuntimeError):  # noqa: N818 — named for what the operator sees
    """The run in this directory cannot be scored as asked: it holds no run,
    is not complete, holds no packets, another run holds it, or a score
    changed once the seal was open."""


@dataclass(frozen=True)
class Place:
    """Where a run drew its packets, and what scoring needs to know of it."""

    run: str  # "scored" or "practice"
    window: int | None
    directory: Path  # the packets, the raters' files, the seal and the pairs
    judging: Path
    batch: str
    arms: tuple[str, ...]
    kept: tuple[str, ...]  # the series every arm's comparisons keep
    due: dt.datetime | None


@dataclass(frozen=True)
class Progress:
    """How far scoring has got, the seal still shut."""

    packets: int
    left: dict[str, tuple[str, ...]]  # each rater's packets not yet scored, in their order
    written: tuple[str, ...]  # the people whose blank sheet was written this time
    sheets: dict[str, Path]
    judge_cap_reached: bool
    due: dt.datetime | None  # the last moment scoring finishes on time

    @property
    def complete(self) -> bool:
        return not any(self.left.values())


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def score(
    root: Path,
    *,
    materials: Materials,
    prompts: JudgePrompts,
    open_seal: bool = False,
    now: Callable[[], dt.datetime] = _real_now,
) -> Progress | dict[str, Any]:
    """Score the run in *root*: how far scoring has got while the seal is
    shut, or the result once it is open. *open_seal* opens it, once every
    score is in; asked before, it opens nothing. *prompts* are the judge's,
    which its kept answers were asked with. *now* is real time."""
    if not (root / scored.RUN).is_file():
        raise ScoringRefused(f"{root}: no run is held in this directory")
    with sole_run(root, ScoringRefused(f"{root}: another run is holding this directory")):
        place = _locate(root)
        try:
            drawn = read_packets(place.directory)
        except FileNotFoundError:
            raise ScoringRefused(f"{root}: no packets were drawn in this run") from None
        opened = _read_opened(place.directory)
        written = () if opened else write_blank_sheets(place.directory, drawn)
        sheets = {p: read_sheet(sheet_path(place.directory, p), drawn, p) for p in PEOPLE}
        judged = read_batch(drawn.order(RATER), prompts, place.judging, batch=place.batch)
        if opened is None:
            progress = _progress(place, drawn, sheets, judged, written)
            if not open_seal or not progress.complete:
                return progress
            at = now()
        else:
            _unchanged(place, sheets, opened)
            at = dt.datetime.fromisoformat(opened["at"])
        assert judged is not None  # every score was in when the seal was opened
        report = _result(place, materials, drawn, _seal(place.directory), sheets, judged, at)
        if opened is None:  # kept only once the result is, from the scores it was worked from
            write_json(place.directory / OPENED, {"at": at.isoformat(), **_held(place, sheets)})
        write_json(root / RESULT, report)
        (root / RESULT_SUMMARY).write_text(result_report.summary(report))
        return report


def _locate(root: Path) -> Place:
    """Where the run in *root* drew its packets; a run with none to score is refused."""
    state = json.loads((root / scored.RUN).read_text())
    if "windows" not in state:
        if state.get("fault"):
            raise ScoringRefused(f"{root}: a harness fault closed this practice run")
        return Place("practice", None, root, root / practice.JUDGING / practice.BATCH,
                     practice.BATCH, tuple(state["arms"]), (PRACTICE_SERIES,), None)
    windows = state["windows"]
    stands, why = scored_report.outcome(windows)
    last = windows[-1]
    if stands != scored_report.COMPLETE or not scored_report.is_scored(last):
        raise ScoringRefused(f"{root}: the scored run is {stands}" + (f": {why}" if why else "")
                             + "; nothing it drew can be scored")
    directory = window_directory(root, last["window"])
    if last["ended"]["last_scored_meeting_at"] is None:
        raise ScoringRefused(f"{root}: no scored meeting ended in the scored run; nothing it "
                             "drew can be scored")
    ended = dt.datetime.fromisoformat(last["ended"]["last_scored_meeting_at"])
    return Place("scored", last["window"], directory, directory / JUDGING / BATCH, BATCH, ARMS,
                 tuple(last["ended"]["kept"]), ended + scored_report.SCORING_DAYS)


def _progress(
    place: Place, drawn: Drawn, sheets: Mapping[str, Sheet], judged: Judged | None,
    written: tuple[str, ...],
) -> Progress:
    done = answered(place.judging) if judged is None else frozenset(drawn.packets)
    return Progress(
        packets=len(drawn.packets),
        left={**{p: sheets[p].left for p in PEOPLE},
              RATER: tuple(p for p in drawn.orders[RATER] if p not in done)},
        written=written,
        sheets={p: sheet_path(place.directory, p) for p in PEOPLE},
        judge_cap_reached=judged is not None and judged.cap_reached,
        due=place.due,
    )


def _seal(directory: Path) -> Seal:
    try:
        return read_seal(directory)
    except FileNotFoundError:
        raise ScoringRefused(f"{directory}: no seal is kept beside the packets") from None


def _held(place: Place, sheets: Mapping[str, Sheet]) -> dict[str, Any]:
    """What the result was worked from: each person's scores as their sheet
    gave them, so the same scores saved again still read, and every file the
    harness wrote that the result reads."""
    files = {
        "judge": place.judging / REPLIES,
        "judge_calls": place.judging / CALL_LOG,
        "packets": place.directory / PACKETS,
        "seal": place.directory / SEALED / SEAL,
    }
    return {
        "sheets": {p: _scores_sha256(sheets[p]) for p in PEOPLE},
        "files": {key: _sha256(path) for key, path in files.items()},
    }


def _scores_sha256(sheet: Sheet) -> str:
    scores = json.dumps({"memos": sheet.memos, "recall": sheet.recall}, sort_keys=True)
    return hashlib.sha256(scores.encode()).hexdigest()


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _read_opened(directory: Path) -> dict[str, Any] | None:
    try:
        return dict(json.loads((directory / OPENED).read_text()))
    except FileNotFoundError:
        return None


def _unchanged(place: Place, sheets: Mapping[str, Sheet], opened: Mapping[str, Any]) -> None:
    """Refuse once any score, or any file the result reads, has changed
    since the seal was opened."""
    now = _held(place, sheets)
    for person in PEOPLE:
        if now["sheets"][person] != opened["sheets"][person]:
            raise ScoringRefused(
                f"{person}'s sheet changed after the seal was opened at {opened['at']}; "
                "no score may change once the arms are known",
            )
    for key, what in _PINNED.items():
        if now["files"][key] != opened["files"][key]:
            raise ScoringRefused(f"{what} changed after the seal was opened at {opened['at']}")


def _result(
    place: Place, materials: Materials, drawn: Drawn, seal: Seal, sheets: Mapping[str, Sheet],
    judged: Judged, opened_at: dt.datetime,
) -> dict[str, Any]:
    """Read every score back through the seal, and work out the result."""
    by_id = {s.id: s for s in (*materials.series, materials.practice)}
    kept = [by_id[sid] for sid in place.kept]
    _check_seal(kept, place.arms, seal)
    raters = PEOPLE if judged.cap_reached else RATERS
    runs = {(s.id, arm): _pair(place, s, arm) for s in kept for arm in place.arms}
    ratings = _ratings(drawn, seal, sheets, judged, raters)
    on_time = None if place.due is None else opened_at <= place.due
    rows = _meetings(kept, runs, seal)
    figures = build(
        series=kept, arms=place.arms, ratings=ratings, raters=raters,
        dollars=_dollars(place, kept, runs, repricing=False),
        repriced=_dollars(place, kept, runs, repricing=True),
        minutes={MemoRef(r["arm"], r["series"], r["meeting"]): r["minutes"] for r in rows},
        run_complete=True, on_time=on_time, judge_cap_reached=judged.cap_reached,
    )
    return {
        "run": place.run, "window": place.window, "opened_at": opened_at.isoformat(),
        "scoring_due": None if place.due is None else place.due.isoformat(),
        "on_time": on_time, **figures, "meetings": rows,
    }


def _check_seal(kept: Sequence[Series], arms: Sequence[str], seal: Seal) -> None:
    """The seal names every answer the kept series asked of every arm, once
    each, as a packet drawn or as an answer missing."""
    asked = {MemoRef(a, s.id, m.id) for s in kept for a in arms for m in s.meetings
             if m.kind is not MeetingKind.BRIEFING}
    named = [*seal.packets.values(), *seal.missing]
    if set(named) != asked or len(named) != len(asked):
        raise ScoringRefused("the seal does not name every answer the kept series asked for, "
                             "once each")


def _ratings(
    drawn: Drawn, seal: Seal, sheets: Mapping[str, Sheet], judged: Judged,
    raters: Sequence[str],
) -> Ratings:
    """Every rater's scores by the memo or reply they score."""
    memos: dict[MemoRef, dict[str, dict[str, int | None]]] = {}
    recall: dict[MemoRef, dict[str, dict[str, bool]]] = {}
    if set(seal.packets) != set(drawn.packets):
        raise ScoringRefused("the seal does not name the packets that were drawn")
    for pid, ref in seal.packets.items():
        if isinstance(drawn.packets[pid], RecallPacket):
            recall[ref] = {p: sheets[p].recall[pid] for p in PEOPLE}
            if RATER in raters:
                recall[ref][RATER] = judged.recall_marks[pid]
        else:
            memos[ref] = {p: sheets[p].memos[pid] for p in PEOPLE}
            if RATER in raters:
                memos[ref][RATER] = judged.memo_scores[pid].scores
    return Ratings(memos=memos, recall=recall, packets={r: p for p, r in seal.packets.items()})


def _pair(place: Place, series: Series, arm: str) -> SeriesRun[Kept]:
    run = read_pair(place.directory / PAIRS / series.id / arm)
    if run is None or run.finished_attempt is None:
        raise ScoringRefused(f"{arm}'s {series.id} was not held to the end, so it is kept in no "
                             "comparison")
    return run


def _dollars(
    place: Place, kept: Sequence[Series], runs: Mapping[tuple[str, str], SeriesRun[Kept]], *,
    repricing: bool,
) -> dict[tuple[str, str], float]:
    """Each arm's dollars per plan in each kept series, as the scored run's
    report counts them; a practice series' over its own plans, not four."""
    out = {}
    for s in kept:
        for arm in place.arms:
            run = runs[(s.id, arm)]
            records = pair_calls(place.directory / PAIRS / s.id / arm, run).records
            assert run.finished_attempt is not None  # _pair refuses a dropped series
            try:
                per_plan = dollars_per_plan(
                    repriced(records) if repricing else records, arm=arm, series=s.id,
                    attempt=run.finished_attempt, tries=run.finished_tries(),
                    prices=REPRICING_PRICES if repricing else PRICES,
                )
            except (KeyError, ValueError) as exc:
                raise ScoringRefused(
                    f"{arm}'s {s.id}: a call the fixed table cannot price ({exc})",
                ) from exc
            out[(arm, s.id)] = per_plan * PLANS_PER_SCORED_SERIES / len(s.scored_meetings)
    return out


def _meetings(
    kept: Sequence[Series], runs: Mapping[tuple[str, str], SeriesRun[Kept]], seal: Seal,
) -> list[dict[str, Any]]:
    """Each meeting's finished try, as part 2 §7 reports it: what closed
    the discussion, each adviser's energy at the memo turn, whether the memo
    was cut at 400 words, and a plan's minutes, from the operator's message
    to the finished memo."""
    rows = []
    for s in kept:
        for arm in ARMS:
            if (s.id, arm) not in runs:
                continue
            finished = runs[(s.id, arm)].finished()
            for meeting in s.meetings:
                ref = MemoRef(arm, s.id, meeting.id)
                held = finished[meeting.id].held.result
                record: Mapping[str, Any] = {} if held is None else held.record
                written = held is not None and held.answer is not None
                cut = seal.cuts.get(ref)
                rows.append({
                    **ref._asdict(), "kind": meeting.kind.value,
                    "closed_by": record.get("closed_by"),
                    "energy": record.get("energy"),
                    "minutes": _minutes(arm, record) if meeting.kind.scored and written else None,
                    "words": None if cut is None else cut.words,
                    "cut": None if cut is None else cut.cut,
                })
    return rows


def _minutes(arm: str, record: Mapping[str, Any]) -> float:
    """From the operator's message to the finished memo: arm A's one call,
    or a channel arm's opening message to its chair's memo."""
    if arm == "A":
        asked, done = record["asked_at"], record["answered_at"]
    else:
        asked, done = record["opened"]["at"], record["memo"]["at"]
    spent = dt.datetime.fromisoformat(done) - dt.datetime.fromisoformat(asked)
    return spent.total_seconds() / 60
