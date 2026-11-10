"""EXP-001's AI raters, who score in the two people's place.

On 2026-11-10 the maintainer decided that two AI raters, not two people,
score EXP-001, and that the result says so first
(``docs/experiments/EXP-001-deviation-ai-raters.md``). Rater 1 fills
person-1's sheet from person-1's packets, and rater 2 person-2's. Each
rater's own session briefs it with this module, never reading a packet:

- :func:`write_batches` splits the rater's packet file into batches of
  :data:`BATCH_SIZE`, in the file's own order. Each batch becomes one
  prompt: the rubric as the people have it, the notes agreed in practice,
  the batch's packets, and the cells to score for each, taken from the
  rater's blank sheet. Beside it a small JSON file records what it asks.
  A fresh run reads one prompt and answers it.
- :func:`enter` reads that answer from the run's own record, as Claude Code
  keeps it, and enters it in the rater's sheet exactly as given. The record
  must show replies from the rater's model alone, and no tool call but one
  Read of the prompt and the hand-back of the answer. An answer that does
  not name that model, or does not score exactly the cells its prompt asked
  for with 0, 1 or 2, or right or wrong, is refused and enters nothing, so
  the run is held again whole. A cell already scored is never scored again,
  and nothing is entered once the seal is open.
- :func:`differences` lists the cells two raters scored differently, on a
  practice run only, so they can agree how the anchors apply.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from evaluators.exp001 import scored
from evaluators.exp001.judge import criteria_text
from evaluators.exp001.rating import PEOPLE, RATER_FILES
from evaluators.exp001.scoring_run import OPENED
from evaluators.exp001.sheets import NOT_SCORED, sheet_path

BATCH_SIZE = 8
ENTERED = "entered.jsonl"
_FIXED = 3  # number, packet and kind come before the cells to score
_HEADING = re.compile(r"^=== Packet (\S+), (\d+) of (\d+) ===$", re.M)
_MODEL = re.compile(r"\A\s*model:\s*(\S+)\s*$", re.M)
_JSON = re.compile(r"```json\s*\n(.*?)```", re.S)
_NO_NOTES = "None yet: this is the practice."
_HANDBACK = "SubagentHandback"  # the tool a subagent hands its answer back with

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


class RaterRefused(ValueError):  # noqa: N818 — named for what the operator sees
    """What the rater's session asked would break the rater rules, so nothing was done."""


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
    header, rows = _read_sheet(directory, person)
    packets = split((directory / RATER_FILES / f"{person}.txt").read_text())
    if [pid for pid, _ in packets] != [row[1] for row in rows]:
        raise RaterRefused(f"{person}'s packets are not the packets their sheet lists, in order")
    if any(cell not in ("", NOT_SCORED) for row in rows for cell in row[_FIXED:]):
        raise RaterRefused(f"{person}'s sheet already holds scores; batches are written "
                           "once, before any")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.glob("batch-*")):
        raise RaterRefused(f"{out} holds batches already; each is written once")
    doc = yaml.safe_load(rubric.read_text())
    notes_text = notes.read_text().strip() if notes is not None else _NO_NOTES
    cells = {row[1]: [c for c, cell in zip(header[_FIXED:], row[_FIXED:], strict=True)
                      if cell == ""] for row in rows}
    written = []
    for start in range(0, len(packets), size):
        batch = packets[start:start + size]
        name = f"batch-{start // size + 1:02d}"
        prompt = _PROMPT.format(
            scale=doc["memo"]["scale"], length=doc["memo"]["length"], order=doc["memo"]["order"],
            criteria=criteria_text(doc["memo"]["criteria"]), rule=doc["recall"]["rule"],
            notes=notes_text, count=len(batch),
            packets="\n".join(f"{text.rstrip()}\n\nCells to score for {pid}: "
                              f"{', '.join(cells[pid])}\n" for pid, text in batch),
        )
        path = out / f"{name}.md"
        with path.open("x") as handle:
            handle.write(prompt)
        _write_new(path.with_suffix(".json"), {
            "person": person, "batch": name, "prompt_sha256": _sha256(prompt.encode()),
            "notes_sha256": None if notes is None else _sha256(notes.read_bytes()),
            "packets": [pid for pid, _ in batch], "cells": {pid: cells[pid] for pid, _ in batch},
        })
        written.append(path)
    return written


def enter(
    directory: Path, person: str, prompt: Path, record: Path, *, model: str,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> tuple[str, ...]:
    """Enter the answer to *prompt* that the run's *record* holds into
    *person*'s sheet in *directory*, exactly as given; the packets entered.
    The answer is kept beside the record. *model* is the only model the run
    may have replied with."""
    if (directory / OPENED).exists():
        raise RaterRefused("the seal is open, so no score may be entered")
    asked = json.loads(prompt.with_suffix(".json").read_text())
    if asked["person"] != person or _sha256(prompt.read_bytes()) != asked["prompt_sha256"]:
        raise RaterRefused(f"{prompt} changed after it was written, or is not {person}'s")
    text = _ran(record, prompt, model)
    answer = record.with_suffix(".answer.md")
    answer.write_text(text)
    ran_on = _model(text)
    if ran_on != model:
        raise RaterRefused(f"the run says it ran on {ran_on}, not {model}")
    given = _scores(text, asked["cells"])
    header, rows = _read_sheet(directory, person)
    by_packet = {row[1]: row for row in rows}
    for pid, cells in given.items():
        row = by_packet[pid]
        for cell, value in cells.items():
            column = header.index(cell)
            if row[column] != "":
                raise RaterRefused(f"{pid}: {cell} is already scored; a score is never "
                                   "scored again")
            row[column] = str(value)
    _write_sheet(directory, person, header, rows)
    with (prompt.parent / ENTERED).open("a") as log:
        log.write(json.dumps({
            "batch": asked["batch"], "model": ran_on, "packets": asked["packets"],
            "prompt_sha256": asked["prompt_sha256"],
            "record": record.name, "record_sha256": _sha256(record.read_bytes()),
            "answer": answer.name, "answer_sha256": _sha256(answer.read_bytes()),
            "at": now().isoformat(),
        }) + "\n")
    return tuple(given)


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


def _ran(record: Path, prompt: Path, model: str) -> str:
    """The answer in a run's *record*, once the record shows the run replied
    with *model* alone and read its *prompt* and nothing else."""
    replies = []
    for line in record.read_text().splitlines():
        try:
            entry = json.loads(line) if line.strip() else {}
        except json.JSONDecodeError:
            raise RaterRefused(f"{record}: a line of the run's record cannot be read") from None
        if entry.get("type") in ("assistant", "user"):
            replies.append(entry)
    models = {e["message"].get("model") for e in replies if e["type"] == "assistant"}
    if models != {model}:
        raise RaterRefused(f"the run's record shows replies from {', '.join(map(str, models))}"
                           f", not {model} alone")
    calls = [block for e in replies if e["type"] == "assistant"
             for block in _blocks(e) if block.get("type") == "tool_use"]
    handback = [c for c in calls if c.get("name") == _HANDBACK]
    read = [c for c in calls if c.get("name") != _HANDBACK]
    if (len(read) != 1 or read[0].get("name") != "Read" or len(handback) > 1
            or Path(str(read[0].get("input", {}).get("file_path"))).resolve() != prompt.resolve()):
        raise RaterRefused("the run's record shows a tool call other than one Read of its "
                           "prompt and the hand-back of its answer")
    if handback:
        return str(handback[0].get("input", {}).get("message", ""))
    last_said = max(i for i, e in enumerate(replies) if e["type"] == "user")
    return "".join(block.get("text", "") for e in replies[last_said + 1:]
                   for block in _blocks(e) if block.get("type") == "text")


def _blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    content = entry["message"].get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _model(text: str) -> str:
    found = _MODEL.match(text)
    if found is None:
        raise RaterRefused("the answer does not start with its model line, `model: <id>`")
    return found.group(1)


def _scores(text: str, asked: dict[str, list[str]]) -> dict[str, dict[str, int | str]]:
    """The answer's JSON block, checked against the cells *asked*."""
    blocks = _JSON.findall(text)
    if not blocks:
        raise RaterRefused("the answer ends with no ```json block")
    try:
        given = json.loads(blocks[-1])
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
    staged = path.with_name(path.name + ".new")
    with staged.open("w", newline="") as sheet:
        csv.writer(sheet).writerows([header, *rows])
    os.replace(staged, path)


def _write_new(path: Path, content: Any) -> None:
    with path.open("x") as handle:
        json.dump(content, handle, indent=2)
        handle.write("\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
