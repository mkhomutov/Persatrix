"""EXP-001's AI raters, who score in the two people's place.

On 2026-11-10 the maintainer decided that two AI raters, not two people,
score EXP-001, and that the result says so first
(``docs/experiments/EXP-001-deviation-ai-raters.md``). Rater 1 fills
person-1's sheet from person-1's packets, and rater 2 person-2's. Each
rater's own session briefs it with this module, never reading a packet:

- :func:`write_batches` splits the rater's packet file into batches of up
  to :data:`BATCH_SIZE`, in the file's own order, and fewer when that many
  would make a prompt too long for one Read. Each batch becomes one prompt:
  the rubric as the people have it, the notes agreed in practice (none in
  the practice itself, and always on a scored run), the batch's packets,
  and the cells to score for each, taken from the rater's blank sheet.
  Beside it a small JSON file records what it asks.
- A fresh run is given :func:`~evaluators.exp001.ai_rater_runs.brief` and
  nothing else, reads its prompt and answers it.
- :func:`enter` copies the run's record beside the prompt, checks it
  (:mod:`evaluators.exp001.ai_rater_runs`) and enters the answer in the
  rater's sheet exactly as given. An answer that does not score exactly the
  cells its prompt asked for, with 0, 1 or 2, or right or wrong, and could
  be read only one way, is refused and enters nothing, so the run is held
  again whole. A cell already scored is never scored again, and nothing is
  entered once the seal is open. Every run, entered or refused, is logged
  in :data:`RUNS` with why, and its record is kept with a published copy.
- :func:`keep` checks and keeps a run that writes the practice notes.
- :func:`unentered` lists a session's runs that were never logged, and
  :func:`same_notes` checks both raters' scored prompts hold one set of
  notes.
- :func:`differences` lists the cells two raters scored differently, on a
  practice run only, so they can agree how the anchors apply.
"""

from __future__ import annotations

import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import yaml

from evaluators.exp001 import scored
from evaluators.exp001.ai_rater_runs import (
    MODEL,
    RaterRefused,
    agent_of,
    brief,
    brief_of,
    checked,
    entries,
    public,
)
from evaluators.exp001.judge import criteria_text
from evaluators.exp001.rating import PEOPLE, RATER_FILES
from evaluators.exp001.scoring_run import OPENED
from evaluators.exp001.sheets import NOT_SCORED, sheet_path

__all__ = [
    "BATCH_SIZE", "PROMPT_LIMIT", "RUNS", "RaterRefused", "differences", "enter", "keep",
    "same_notes", "split", "unentered", "write_batches",
]

BATCH_SIZE = 8
PROMPT_LIMIT = 50_000  # characters; one Read gave 59 000 whole and cut 72 000 short
LINE_LIMIT = 1_800  # lines; one Read gives 2 000 at most
RUNS = "runs.jsonl"
_FIXED = 3  # number, packet and kind come before the cells to score
_HEADING = re.compile(r"^=== Packet (\S+), (\d+) of (\d+) ===$", re.M)
_JSON = re.compile(r"```json\s*\n(.*?)```", re.S)
_NO_NOTES = "None yet: this is the practice."
_T = TypeVar("_T")

_PROMPT = """\
You are one of two raters scoring answers written for fictional
organisations. You score alone. Score only what the packets below show. Do
not guess who or what wrote any answer.

This file is your whole task. Read no other file and use no other tool.

Start your reply with one line naming the exact model ID you run on, as
`model: <id>`.

## How to score a memo

{scale}

{length}

{order}

{criteria}

## How to mark a recall reply

{rule}

## Notes on how the anchors apply, agreed in practice

{notes}

## The packets

{count} packets follow, in your order. Each ends with the cells to score.

{packets}

## Your reply

After the model line, give one short reason for each cell. End your reply
with one JSON block, fenced as ```json, that maps each packet ID above to
its cells: a memo's criteria as the numbers 0, 1 or 2, and a recall reply's
questions as "right" or "wrong". For example:

```json
{{"0a1b2c3d": {{"C1": 2, "C2": 0, "C3": 2, "C4": 1, "C5": 2}},
 "4e5f6a7b": {{"R1": "right", "R2": "wrong"}}}}
```

Give every packet above, every cell listed for it, and nothing else.
"""


def split(text: str) -> list[tuple[str, str]]:
    """A packet file's packets, each its ID and its text from its heading on."""
    headings = list(_HEADING.finditer(text))
    if not headings or headings[0].start() != 0:
        raise RaterRefused("a packet file starts with its first packet's heading")
    total = int(headings[0].group(3))
    if [(int(h.group(2)), int(h.group(3))) for h in headings] != [
        (n, total) for n in range(1, total + 1)
    ]:
        raise RaterRefused(f"the packet headings do not count from 1 to {total}")
    ends = [h.start() for h in headings[1:]] + [len(text)]
    return [(h.group(1), text[h.start():end]) for h, end in zip(headings, ends, strict=True)]


def write_batches(
    directory: Path, person: str, out: Path, *, rubric: Path, notes: Path | None,
    size: int = BATCH_SIZE,
) -> list[Path]:
    """Write *person*'s packets in *directory* to *out* as one prompt per batch."""
    practice = _practice(directory)
    if practice and notes is not None:
        raise RaterRefused("the practice is scored with no notes: they come from it")
    if not practice and notes is None:
        raise RaterRefused("a scored run is scored with the notes agreed in practice: "
                           "give --notes")
    header, rows = _read_sheet(directory, person)
    packets = split((directory / RATER_FILES / f"{person}.txt").read_text())
    if [pid for pid, _ in packets] != [row[1] for row in rows]:
        raise RaterRefused(f"{person}'s packets are not the packets their sheet lists, in order")
    if any(cell not in ("", NOT_SCORED) for row in rows for cell in row[_FIXED:]):
        raise RaterRefused(f"{person}'s sheet already holds scores; batches are written "
                           "once, before any")
    if any(out.glob("batch-*")):
        raise RaterRefused(f"{out} holds batches already; each is written once")
    doc = yaml.safe_load(rubric.read_text())
    notes_text = notes.read_text().strip() if notes is not None else _NO_NOTES
    cells = {row[1]: [c for c, cell in zip(header[_FIXED:], row[_FIXED:], strict=True)
                      if cell == ""] for row in rows}

    def prompt_of(batch: list[tuple[str, str]]) -> str:
        return _PROMPT.format(
            scale=doc["memo"]["scale"], length=doc["memo"]["length"], order=doc["memo"]["order"],
            criteria=criteria_text(doc["memo"]["criteria"]), rule=doc["recall"]["rule"],
            notes=notes_text, count=len(batch),
            packets="\n".join(f"{text.rstrip()}\n\nCells to score for {pid}: "
                              f"{', '.join(cells[pid])}\n" for pid, text in batch),
        )

    batches: list[list[tuple[str, str]]] = []
    for packet in packets:
        if not _fits(prompt_of([packet])):
            raise RaterRefused(f"packet {packet[0]} alone makes a prompt too long for one Read")
        if batches and len(batches[-1]) < size and _fits(prompt_of([*batches[-1], packet])):
            batches[-1].append(packet)
        else:
            batches.append([packet])
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for number, batch in enumerate(batches, 1):
        path = out / f"batch-{number:02d}.md"
        prompt = prompt_of(batch)
        with path.open("x") as handle:
            handle.write(prompt)
        _write_new(path.with_suffix(".json"), {
            "person": person, "batch": path.stem, "prompt_sha256": _sha256(prompt.encode()),
            "notes_sha256": None if notes is None else _sha256(notes.read_bytes()),
            "packets": [pid for pid, _ in batch], "cells": {pid: cells[pid] for pid, _ in batch},
        })
        written.append(path)
    return written


def enter(
    directory: Path, person: str, prompt: Path, record: Path, *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> tuple[str, ...]:
    """Enter the answer to *prompt* that the run's *record* holds into
    *person*'s sheet in *directory*, exactly as given; the packets entered.
    The record, its published copy and the answer are kept beside the
    prompt, and the run is logged, entered or refused."""
    if (directory / OPENED).exists():
        raise RaterRefused("the seal is open, so no score may be entered")
    asked = _asked(prompt)
    if asked["person"] != person or _sha256(prompt.read_bytes()) != asked["prompt_sha256"]:
        raise RaterRefused(f"{prompt} changed after it was written, or is not {person}'s")

    def use(answer: str, _: Path) -> tuple[str, ...]:
        given = _scores(answer, asked["cells"])
        with _locked(sheet_path(directory, person).parent):
            header, rows = _read_sheet(directory, person)
            by_packet = {row[1]: row for row in rows}
            for pid, cells in given.items():
                row = by_packet.get(pid)
                if row is None:
                    raise RaterRefused(f"{pid} is not on {person}'s sheet")
                for cell, value in cells.items():
                    column = header.index(cell)
                    if row[column] != "":
                        raise RaterRefused(f"{pid}: {cell} is already scored; a score is "
                                           "never scored again")
                    row[column] = str(value)
            _write_sheet(directory, person, header, rows)
        return tuple(given)

    return _run(prompt, record, use, now, outcome="entered",
                logged={"packets": asked["packets"], "prompt_sha256": asked["prompt_sha256"]})


def keep(
    prompt: Path, record: Path, *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> Path:
    """Keep the answer to a notes *prompt* that the run's *record* holds,
    checked as a scoring run is; the kept answer's path."""
    if prompt.with_suffix(".json").exists():
        raise RaterRefused(f"{prompt} is a batch: its run is entered with rater-enter")
    return _run(prompt, record, lambda _, answer: answer, now, outcome="kept",
                logged={"prompt_sha256": _sha256(prompt.read_bytes())})


def unentered(out: Path, records: Path) -> list[str]:
    """Each run in *records*, a session's folder of run records, briefed with
    a prompt in *out* but never logged as entered, kept or refused."""
    prompts = {brief(p): p for p in out.glob("*.md") if not p.name.endswith(".answer.md")}
    logged = {r.get("agent") for r in _log(out)}
    listed = []
    for path in sorted(records.glob("*.jsonl")):
        try:
            lines = entries(path)
        except RaterRefused:
            continue
        prompt = prompts.get(brief_of(lines) or "")
        if prompt is not None and (agent_of(lines) or path.stem) not in logged:
            listed.append(f"{path}: a run of {prompt.stem} never entered, kept or refused")
    return listed


def same_notes(outs: Sequence[Path]) -> str:
    """The hash of the one set of notes every scored prompt in *outs* holds."""
    held = {side: json.loads(side.read_text()).get("notes_sha256")
            for out in outs for side in sorted(out.glob("batch-*.json"))}
    found = set(held.values())
    if not held or len(found) != 1 or None in found:
        raise RaterRefused("the raters' batches were not all written with one set of notes: "
                           + ", ".join(f"{side} {sha}" for side, sha in held.items()))
    return str(found.pop())


def differences(root: Path) -> list[str]:
    """Each cell the two raters scored differently on the practice run in *root*."""
    if "windows" in json.loads((root / scored.RUN).read_text()):
        raise RaterRefused("differences are listed only for a practice run; a scored run's "
                           "sheets are compared only by its result")
    first, second = PEOPLE
    header, rows = _read_sheet(root, first)
    other = {row[1]: row for row in _read_sheet(root, second)[1]}
    listed = []
    for row in rows:
        for column, (one, two) in enumerate(zip(row, other[row[1]], strict=True)):
            if column >= _FIXED and one not in ("", NOT_SCORED) and two != one and two:
                listed.append(f"{row[1]} {header[column]}: {first} {one}, {second} {two}")
    return listed


def _run(
    prompt: Path, record: Path, use: Callable[[str, Path], _T], now: Callable[[], dt.datetime],
    *, outcome: str, logged: dict[str, Any],
) -> _T:
    """Copy one run's *record* beside its *prompt*, check it, keep its
    published copy and answer, and *use* the answer and its kept file; log
    the run either way."""
    out = prompt.parent
    with _locked(out):
        number = 1 + sum(1 for r in _log(out) if r.get("batch") == prompt.stem)
        stem = f"{prompt.stem}.run-{number}"
        copy = out / f"{stem}.jsonl"
        entry: dict[str, Any] = {"batch": prompt.stem, "run": number, "record": str(record),
                                 "at": now().isoformat()}
        try:
            try:
                shutil.copyfile(record, copy)
            except OSError as error:
                raise RaterRefused(f"{record}: the run's record cannot be read ({error})") from None
            entry["record_sha256"] = _sha256(copy.read_bytes())
            entry["agent"] = _agent(copy)
            ran = checked(copy, prompt)
            published = out / f"{stem}.public.jsonl"
            published.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n"
                                         for line in public(entries(copy))))
            answer = out / f"{stem}.answer.md"
            answer.write_text(ran.answer)
            entry.update(agent=ran.agent, model=MODEL, instructions=list(ran.instructions),
                         public_sha256=_sha256(published.read_bytes()),
                         answer_sha256=_sha256(answer.read_bytes()), **logged)
            result = use(ran.answer, answer)
        except RaterRefused as refused:
            _append(out / RUNS, {**entry, "outcome": "refused", "why": str(refused)})
            raise
        _append(out / RUNS, {**entry, "outcome": outcome})
        return result


def _agent(record: Path) -> str | None:
    try:
        return agent_of(entries(record))
    except RaterRefused:
        return None


def _practice(directory: Path) -> bool:
    """Whether *directory* is a practice run; a scored run's window is not."""
    own, root = directory / scored.RUN, directory.parent / scored.RUN
    if own.is_file() and "windows" not in json.loads(own.read_text()):
        return True
    if not own.is_file() and root.is_file() and "windows" in json.loads(root.read_text()):
        return False
    raise RaterRefused(f"{directory} is neither a practice run nor a scored run's window")


def _asked(prompt: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(prompt.with_suffix(".json").read_text()))
    except (OSError, ValueError):
        raise RaterRefused(f"{prompt}: no batch beside it that rater-batches wrote") from None


def _scores(text: str, asked: dict[str, list[str]]) -> dict[str, dict[str, int | str]]:
    """The answer's one JSON block, checked against the cells *asked*."""
    blocks = list(_JSON.finditer(text))
    if not blocks:
        raise RaterRefused("the answer ends with no ```json block")
    if len(blocks) > 1:
        raise RaterRefused("the answer gives more than one ```json block")
    if text[blocks[0].end():].strip():
        raise RaterRefused("the answer goes on after its ```json block")
    try:
        given = json.loads(blocks[0].group(1), object_pairs_hook=_once)
    except json.JSONDecodeError as error:
        raise RaterRefused(f"the answer's JSON block cannot be read: {error}") from None
    if not isinstance(given, dict) or set(given) != set(asked):
        raise RaterRefused("the answer does not score every packet its prompt asked for, "
                           "and only those")
    for pid, cells in given.items():
        if not isinstance(cells, dict) or sorted(cells) != sorted(asked[pid]):
            raise RaterRefused(f"{pid}: the answer does not give exactly the cells asked for, "
                               f"{', '.join(asked[pid])}")
        for cell, value in cells.items():
            if cell.startswith("R"):
                if value not in ("right", "wrong"):
                    raise RaterRefused(f"{pid}: {cell} is {value!r}, not right or wrong")
            elif type(value) is not int or value not in (0, 1, 2):
                raise RaterRefused(f"{pid}: {cell} is {value!r}, not 0, 1 or 2")
    return given


def _once(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose every key is given once, or a refusal."""
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise RaterRefused(f"the answer's JSON gives {key} twice")
        seen.add(key)
    return dict(pairs)


def _fits(prompt: str) -> bool:
    return len(prompt) <= PROMPT_LIMIT and prompt.count("\n") < LINE_LIMIT


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """Hold *folder* alone: one entry at a time reads, changes and writes."""
    handle = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _log(out: Path) -> list[dict[str, Any]]:
    path = out / RUNS
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().split("\n") if line.strip()]


def _append(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a") as log:
        log.write(json.dumps(entry) + "\n")


def _read_sheet(directory: Path, person: str) -> tuple[list[str], list[list[str]]]:
    path = sheet_path(directory, person)
    if not path.is_file():
        raise RaterRefused(f"{path}: no sheet; scoring the run writes it blank first")
    with path.open(newline="", encoding="utf-8-sig") as sheet:
        header, *rows = [row for row in csv.reader(sheet) if any(cell.strip() for cell in row)]
    return header, rows


def _write_sheet(
    directory: Path, person: str, header: Sequence[str], rows: Sequence[Sequence[str]],
) -> None:
    path = sheet_path(directory, person)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".new")
    with os.fdopen(handle, "w", newline="") as sheet:
        csv.writer(sheet).writerows([header, *rows])
    os.replace(staged, path)


def _write_new(path: Path, content: Any) -> None:
    with path.open("x") as handle:
        json.dump(content, handle, indent=2)
        handle.write("\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
