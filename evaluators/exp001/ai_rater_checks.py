"""The checks on EXP-001's AI raters that come before the seal opens.

The raters' commands (:mod:`evaluators.exp001.ai_raters`) check each run as
it is entered. These checks look at all of a rater's runs, or at both
raters, once the runs are in (``docs/experiments/EXP-001-deviation-ai-raters.md``,
rules 6 to 8):

- :func:`unentered` is a session's audit of its own folder of run records.
  It lists every record there that none of the session's logs names,
  whatever its first message was, since a rater's session starts no other
  runs: a fork, or a run briefed in other words, is listed too. It lists a
  logged record that has changed since or is gone, and one that cannot be
  read. A folder with no records in it is refused, not passed.
- :func:`same_notes` checks both raters' scored prompts hold one set of
  notes, as the prompts hold them. It is run before the first scored run,
  when a difference can still be put right.
- :func:`ready` says whether the seal may open. The notes are one set;
  every cell is scored; and each sheet holds exactly what the kept answers
  of its entered runs give, so a score no run gave is found. It returns what
  differs between the runs' set-ups, for the result to report: by then no
  score can be given again.
- :func:`differences` lists the cells two raters scored differently, on a
  practice run only, so they can agree how the anchors apply.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evaluators.exp001 import scored
from evaluators.exp001.ai_rater_runs import RaterRefused, agent_of, brief, brief_of, entries
from evaluators.exp001.ai_raters import (
    asked_by,
    is_practice,
    runs_logged,
    scores_given,
    set_up,
    sheet_rows,
)
from evaluators.exp001.rating import PEOPLE
from evaluators.exp001.sheets import NOT_SCORED

__all__ = ["differences", "ready", "same_notes", "unentered"]

_FIXED = 3  # number, packet and kind come before the cells to score
_SHOWN = 5  # differences named when a sheet has more


def unentered(outs: Sequence[Path], records: Path) -> list[str]:
    """What *records*, a session's folder of run records, shows that the
    logs in *outs*, its folders of prompts, do not: each run never logged,
    and each logged record that changed, is gone or cannot be read."""
    found = sorted(records.glob("*.jsonl")) if records.is_dir() else []
    if not found:
        raise RaterRefused(f"{records}: no run records here; name the session's own folder "
                           "of them")
    logged = [run for out in outs for run in runs_logged(out)]
    last = {run["agent"]: run for run in logged if run.get("agent")}
    prompts = {brief(p): p for out in outs for p in out.glob("*.md")
               if not p.name.endswith(".answer.md")}
    listed, seen = [], set()
    for path in found:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            lines = entries(path)
        except RaterRefused:
            if not any(run.get("record_sha256") == sha for run in logged):
                listed.append(f"{path}: a record that cannot be read, and that no log names")
            continue
        agent = agent_of(lines) or path.stem
        seen.add(agent)
        if agent not in last:
            prompt = prompts.get(brief_of(lines) or "")
            listed.append(f"{path}: a run " + (
                "whose first message is no brief of these folders' prompts, "
                if prompt is None else f"of {prompt.stem} ") + "never entered, kept or refused")
        elif last[agent].get("record_sha256") != sha:
            listed.append(f"{path}: changed since it was logged as {_named(last[agent])}; "
                          "enter it again")
    listed += [f"{run.get('record')}: logged as {_named(run)}, but its record is not in "
               f"{records}" for agent, run in last.items() if agent not in seen]
    return listed


def same_notes(outs: Sequence[Path]) -> str:
    """The hash of the one set of notes every scored prompt in *outs*, the
    two raters' folders of prompts, holds."""
    held = _notes(_folders(outs))
    if len(held) != 1 or None in held:
        raise RaterRefused("the raters' batches were not all written with one set of notes: "
                           + ", ".join(f"{sha} in {' and '.join(sorted(where))}"
                                       for sha, where in held.items()))
    return str(next(iter(held)))


def ready(directory: Path, outs: Sequence[Path]) -> list[str]:
    """Whether the seal of the run in *directory* may open, from the two
    raters' folders of prompts, *outs*; what differs between the runs'
    set-ups, for the result to report."""
    folders = _folders(outs)
    held = _notes(folders)
    if len(held) != 1 or (None in held) != is_practice(directory):
        raise RaterRefused("the raters' batches were not all written with one set of notes, "
                           "or with none in the practice: " + ", ".join(map(str, held)))
    entered: list[tuple[str, dict[str, Any]]] = []
    for person, out in folders.items():
        runs = [run for run in runs_logged(out) if run.get("outcome") == "entered"]
        _as_given(directory, person, out, runs)
        entered += [(person, run) for run in runs]
    return _differing(entered)


def differences(root: Path) -> list[str]:
    """Each cell the two raters scored differently on the practice run in *root*."""
    try:
        practice = "windows" not in json.loads((root / scored.RUN).read_text())
    except (OSError, ValueError):
        practice = False
    if not practice:
        raise RaterRefused("differences are listed only for a practice run; a scored run's "
                           "sheets are compared only by its result")
    first, second = PEOPLE
    header, rows = sheet_rows(root, first)
    other = {row[1]: row for row in sheet_rows(root, second)[1]}
    listed = []
    for row in rows:
        for column, (one, two) in enumerate(zip(row, other[row[1]], strict=True)):
            if column >= _FIXED and one not in ("", NOT_SCORED) and two != one and two:
                listed.append(f"{row[1]} {header[column]}: {first} {one}, {second} {two}")
    return listed


def _named(run: dict[str, Any]) -> str:
    return f"run {run.get('run')} of {run.get('batch')}"


def _batches(out: Path) -> list[Path]:
    """The batch prompts rater-batches wrote in *out*, without what the runs left."""
    return sorted(p for p in out.glob("batch-*.md") if ".run-" not in p.name)


def _folders(outs: Sequence[Path]) -> dict[str, Path]:
    """Each rater's folder of prompts, by the person whose sheet it fills;
    anything but one folder for each of the two is refused."""
    folders: dict[str, Path] = {}
    for out in outs:
        people = {str(asked_by(prompt).get("person")) for prompt in _batches(out)}
        if not people:
            raise RaterRefused(f"{out} holds no batches")
        if len(people) == 1:
            folders.setdefault(people.pop(), out)
    if sorted(folders) != sorted(PEOPLE) or sorted(folders.values()) != sorted(outs):
        raise RaterRefused("name each rater's folder of prompts, once: "
                           + " and ".join(f"{person}'s" for person in PEOPLE))
    return folders


def _notes(folders: dict[str, Path]) -> dict[str | None, set[str]]:
    """Each set of notes the folders' prompts hold, by hash, and whose
    prompts hold it; a prompt changed since it was written is refused."""
    held: dict[str | None, set[str]] = {}
    for person, out in folders.items():
        for prompt in _batches(out):
            asked = asked_by(prompt)
            try:
                same = hashlib.sha256(prompt.read_bytes()).hexdigest() == asked["prompt_sha256"]
            except (OSError, KeyError):
                same = False
            if not same:
                raise RaterRefused(f"{prompt} changed after it was written")
            held.setdefault(asked.get("notes_sha256"), set()).add(f"{person}'s prompts")
    return held


def _as_given(directory: Path, person: str, out: Path, runs: Sequence[dict[str, Any]]) -> None:
    """Refuse unless *person*'s sheet holds, in every cell, exactly what the
    kept answers of its entered *runs* give."""
    gave: dict[tuple[str, str], str] = {}
    for run in runs:
        prompt = out / f"{run.get('batch')}.md"
        kept = out / f"{run.get('batch')}.run-{run.get('run')}.answer.md"
        try:
            answer = kept.read_bytes()
        except OSError:
            answer = b""
        if hashlib.sha256(answer).hexdigest() != run.get("answer_sha256"):
            raise RaterRefused(f"{kept} is not the answer that was logged as {_named(run)}")
        for pid, cells in scores_given(answer.decode(), asked_by(prompt)["cells"]).items():
            gave.update({(pid, cell): str(value) for cell, value in cells.items()})
    header, rows = sheet_rows(directory, person)
    holds = {(row[1], header[column]): cell for row in rows
             for column, cell in enumerate(row) if column >= _FIXED and cell != NOT_SCORED}
    blank = sum(1 for cell in holds.values() if cell == "")
    if blank:
        raise RaterRefused(f"{person}'s sheet has {blank} cells not yet scored")
    wrong = sorted(key for key in holds.keys() | gave.keys() if holds.get(key) != gave.get(key))
    if wrong:  # named by cell, never by score: the seal is still shut
        shown = ", ".join(f"{pid} {cell}" for pid, cell in wrong[:_SHOWN])
        raise RaterRefused(f"{person}'s sheet does not hold what its entered runs gave, in "
                           f"{len(wrong)} cells: {shown}")


def _differing(entered: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    """Each thing that is not the same in every entered run of both raters."""
    seen: dict[str, dict[str, list[str]]] = {}
    for person, run in entered:
        given = run.get("given") or {}
        about = {**set_up(run), "Claude Code's version": str(given.get("version")),
                 "system prompt": str(given.get("system_prompt")),
                 "kinds of thing given": ", ".join(given.get("kinds") or []),
                 "harness commit": str(run.get("harness"))}
        for what, value in about.items():
            seen.setdefault(what, {}).setdefault(value, []).append(f"{person} {_named(run)}")
    return [f"{what} differs between runs: " + "; ".join(
                f"{value} in {len(where)} runs, the first {where[0]}"
                for value, where in sorted(values.items()))
            for what, values in seen.items() if len(values) > 1]
