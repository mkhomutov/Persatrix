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
  entered once the seal is open. A run set up unlike its folder's first, in
  its effort or the maintainer's own instructions, is refused too. Every
  run, entered or refused, is logged in :data:`RUNS` with why, what it was
  given and the harness's commit, and its record is kept with a published
  copy. Nothing kept is ever written over.
- :func:`keep` checks and keeps a run that writes the practice notes, and
  :func:`briefing` gives a prompt's brief once one Read gives it whole.

The checks that look at all of a rater's runs, or at both raters, are in
:mod:`evaluators.exp001.ai_rater_checks`.
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
import subprocess
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
    Run,
    agent_of,
    brief,
    checked,
    entries,
    public,
)
from evaluators.exp001.judge import criteria_text
from evaluators.exp001.rating import RATER_FILES
from evaluators.exp001.scoring_run import OPENED
from evaluators.exp001.sheets import NOT_SCORED, sheet_path

__all__ = [
    "BATCH_SIZE", "LINE_LIMIT", "PROMPT_LIMIT", "RUNS", "RaterRefused", "asked_by", "briefing",
    "enter", "fits", "is_practice", "keep", "runs_logged", "scores_given", "set_up",
    "sheet_rows", "split", "write_batches",
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
questions as "right" or "wrong". Its shape, with every score left out:

    {{"<packet ID>": {{"C1": ..., "C2": ..., "C3": ..., "C4": ..., "C5": ...}},
     "<packet ID>": {{"R1": "...", "R2": "..."}}}}

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
    practice = is_practice(directory)
    if practice and notes is not None:
        raise RaterRefused("the practice is scored with no notes: they come from it")
    if not practice and notes is None:
        raise RaterRefused("a scored run is scored with the notes agreed in practice: "
                           "give --notes")
    header, rows = sheet_rows(directory, person)
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
        if not fits(prompt_of([packet])):
            raise RaterRefused(f"packet {packet[0]} alone makes a prompt too long for one Read")
        if batches and len(batches[-1]) < size and fits(prompt_of([*batches[-1], packet])):
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
            "notes_sha256": None if notes is None else _sha256(notes_text.encode()),
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
    asked: dict[str, Any] = {}

    def check() -> dict[str, Any]:
        if (directory / OPENED).exists():
            raise RaterRefused("the seal is open, so no score may be entered")
        asked.update(asked_by(prompt))
        written = asked.get("prompt_sha256")
        if asked.get("person") != person or _sha256(_prompt_bytes(prompt)) != written:
            raise RaterRefused(f"{prompt} changed after it was written, or is not {person}'s")
        return {"packets": asked.get("packets"), "prompt_sha256": written}

    def use(answer: str, _: Path) -> tuple[str, ...]:
        given = scores_given(answer, asked.get("cells") or {})
        with _locked(sheet_path(directory, person).parent):
            header, rows = sheet_rows(directory, person)
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

    return _run(prompt, record, check, use, now, outcome="entered")


def keep(
    prompt: Path, record: Path, *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> Path:
    """Keep the answer to a notes *prompt* that the run's *record* holds,
    checked as a scoring run is; the kept answer's path."""
    def check() -> dict[str, Any]:
        if prompt.with_suffix(".json").exists():
            raise RaterRefused(f"{prompt} is a batch: its run is entered with rater-enter")
        return {"prompt_sha256": _sha256(_prompt_bytes(prompt))}

    return _run(prompt, record, check, lambda _, answer: answer, now, outcome="kept")


def briefing(prompt: Path) -> str:
    """The one message a fresh run of *prompt* is given, once the prompt is
    there and one Read gives it whole."""
    if not fits(_prompt_bytes(prompt).decode()):
        raise RaterRefused(f"{prompt} is too long for one Read: a prompt keeps to "
                           f"{PROMPT_LIMIT} characters and {LINE_LIMIT} lines")
    return brief(prompt)


def _run(
    prompt: Path, record: Path, check: Callable[[], dict[str, Any]],
    use: Callable[[str, Path], _T], now: Callable[[], dt.datetime], *, outcome: str,
) -> _T:
    """Copy one run's *record* beside its *prompt* and keep its published
    copy. Then *check* what was asked, check the record, keep the answer and
    *use* it and its kept file. The run is logged either way, with what
    *check* found, and nothing kept before is ever written over."""
    out = prompt.parent
    if not out.is_dir():
        raise RaterRefused(f"{prompt}: no such prompt")
    with _locked(out):
        log = runs_logged(out)
        number = 1 + sum(1 for r in log if r.get("batch") == prompt.stem)
        while any(out.glob(f"{prompt.stem}.run-{number}.*")):
            number += 1  # an entry that stopped part-way left files; they stay
        stem = f"{prompt.stem}.run-{number}"
        copy = out / f"{stem}.jsonl"
        entry: dict[str, Any] = {"batch": prompt.stem, "run": number, "record": str(record),
                                 "at": now().isoformat(), "harness": _harness()}
        try:
            try:
                shutil.copyfile(record, copy)
                if record.with_suffix(".meta.json").is_file():
                    shutil.copyfile(record.with_suffix(".meta.json"),
                                    copy.with_suffix(".meta.json"))
            except OSError as error:
                raise RaterRefused(f"{record}: the run's record cannot be read ({error})") from None
            entry["record_sha256"] = _sha256(copy.read_bytes())
            lines = entries(copy)
            entry["agent"] = agent_of(lines)
            published = out / f"{stem}.public.jsonl"
            published.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n"
                                         for line in public(lines)))
            entry["public_sha256"] = _sha256(published.read_bytes())
            entry.update(check())
            ran = checked(copy, prompt)
            _as_the_first(log, ran)
            answer = out / f"{stem}.answer.md"
            answer.write_text(ran.answer)
            entry.update(agent=ran.agent, model=MODEL, instructions=list(ran.instructions),
                         given=ran.given, answer_sha256=_sha256(answer.read_bytes()))
            result = use(ran.answer, answer)
        except RaterRefused as refused:
            _append(out / RUNS, {**entry, "outcome": "refused", "why": str(refused)})
            raise
        _append(out / RUNS, {**entry, "outcome": outcome})
        return result


def set_up(run: dict[str, Any]) -> dict[str, str]:
    """What a logged *run* must share with its folder's first: its effort,
    and each of the maintainer's own instructions files, by hash."""
    own = sorted(f"{f.get('path')} {f.get('sha256')}" for f in run.get("instructions") or []
                 if f.get("type") == "User")
    return {"effort": str((run.get("given") or {}).get("effort")),
            "the maintainer's own instructions": "; ".join(own) or "none"}


def _as_the_first(log: list[dict[str, Any]], ran: Run) -> None:
    """Refuse a run set up unlike the first run this folder entered or kept."""
    first = next((r for r in log if r.get("outcome") in ("entered", "kept")), None)
    if first is None:
        return
    was, now = set_up(first), set_up({"given": ran.given, "instructions": ran.instructions})
    for what, value in now.items():
        if was[what] != value:
            raise RaterRefused(f"the run's {what} ({value}) is not what this folder's first "
                               f"run had ({was[what]}); every run of a rater is set up alike")


def _harness() -> str:
    """The commit of the checkout these commands run from."""
    try:
        return subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _prompt_bytes(prompt: Path) -> bytes:
    try:
        return prompt.read_bytes()
    except OSError:
        raise RaterRefused(f"{prompt}: no such prompt") from None


def is_practice(directory: Path) -> bool:
    """Whether *directory* is a practice run; a scored run's window is not."""
    own, root = directory / scored.RUN, directory.parent / scored.RUN
    if own.is_file() and "windows" not in json.loads(own.read_text()):
        return True
    if not own.is_file() and root.is_file() and "windows" in json.loads(root.read_text()):
        return False
    raise RaterRefused(f"{directory} is neither a practice run nor a scored run's window")


def asked_by(prompt: Path) -> dict[str, Any]:
    """What the batch *prompt* asks, as rater-batches wrote it beside the prompt."""
    try:
        return dict(json.loads(prompt.with_suffix(".json").read_text()))
    except (OSError, ValueError, TypeError):
        raise RaterRefused(f"{prompt}: no batch beside it that rater-batches wrote") from None


def scores_given(text: str, asked: dict[str, list[str]]) -> dict[str, dict[str, int | str]]:
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


def fits(prompt: str) -> bool:
    """Whether one Read gives *prompt* whole."""
    return len(prompt) <= PROMPT_LIMIT and prompt.count("\n") <= LINE_LIMIT


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """Hold *folder* alone: one entry at a time reads, changes and writes."""
    handle = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def runs_logged(out: Path) -> list[dict[str, Any]]:
    """Every run logged in *out*, a folder of prompts, in the order logged."""
    path = out / RUNS
    if not path.is_file():
        return []
    try:
        return [dict(json.loads(line)) for line in path.read_text().split("\n") if line.strip()]
    except (ValueError, TypeError):
        raise RaterRefused(f"{path}: a line of the log cannot be read") from None


def _append(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a") as log:
        log.write(json.dumps(entry) + "\n")


def sheet_rows(directory: Path, person: str) -> tuple[list[str], list[list[str]]]:
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
