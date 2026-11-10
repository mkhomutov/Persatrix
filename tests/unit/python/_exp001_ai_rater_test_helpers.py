"""The AI-rater tests' shared pieces: the runs they score, and each run's
record in the shape Claude Code keeps it.

The tests score a practice run, whose packets anyone may read, and a copy
of it laid out as a scored run's window.

Claude Code 2.1 keeps one record per run a session starts: a JSON object
per line. A run's first line is its brief; Claude Code then adds what the
run is given besides it (its environment, its model, the instructions
files, the date, the account), one ``attachment`` line each, and the system
reminder that tells a run to hand its answer back. Then come its replies,
each tool call on a line of its own, and each tool's result. Beside the
record Claude Code keeps a small file that names the run's agent type. The
shape here follows a real record of a Fable 5.2 run that read a batch
prompt and handed its answer back; every value is made up.

Kept apart from the tests so that every AI-rater test module writes the
same shape. The pattern mirrors ``_exp001_scores_test_helpers.py``: a
private module beside the test files, imported by name.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001 import ai_raters
from evaluators.exp001.ai_rater_runs import MODEL, brief
from evaluators.exp001.ai_raters import RUNS, write_batches
from evaluators.exp001.materials import load_materials
from evaluators.exp001.scoring_run import score

from ._exp001_judge_test_helpers import PROMPTS
from ._exp001_run_test_helpers import EXP
from ._exp001_scores_test_helpers import build_practice

__all__ = [
    "RUBRIC", "answer", "as_window", "attached", "batches", "enter_answer", "first_memo",
    "given", "held_practice", "notes_file", "out_of", "record", "replied", "runs", "said",
    "scores", "sidecar",
]

RUBRIC = EXP / "rubric.yaml"

FOLDER = "/Users/rater/exp001/ai-raters/person-1"
REMINDER = ("<system-reminder>\nYour final report is delivered through SubagentHandback: "
            "when your work is complete, call SubagentHandback({message: <your full "
            "report>}).\n</system-reminder>")
INSTRUCTIONS = [{"path": "/Users/rater/.claude/CLAUDE.md", "type": "User",
                 "content": "# My own notes\n"}]


def _line(kind: str, **fields: Any) -> dict[str, Any]:
    return {"parentUuid": None, "isSidechain": True, "agentId": "", "type": kind,
            "uuid": f"u-{kind}", "timestamp": "2026-11-11T00:00:00.000Z",
            "userType": "external", "entrypoint": "claude-desktop", "cwd": FOLDER,
            "sessionId": "s-1", "version": "2.1.317", **fields}


def attached(kind: str, **fields: Any) -> dict[str, Any]:
    """One thing Claude Code gives a run besides its brief."""
    return _line("attachment", attachment={"type": kind, **fields})


def said(content: Any, **fields: Any) -> dict[str, Any]:
    """A message to the run, or the results of its tool calls."""
    return _line("user", message={"role": "user", "content": content}, **fields)


def replied(block: dict[str, Any], model: str, effort: str = "high") -> dict[str, Any]:
    """One block of a run's reply, on a line of its own."""
    return _line("assistant", requestedModel=model, effort=effort, perTurnEffort=effort, message={
        "model": model, "id": "msg_1", "type": "message", "role": "assistant",
        "content": [block], "stop_reason": "tool_use", "usage": {"output_tokens": 1},
    })


def _numbered(text: str) -> str:
    """A file as the Read tool gives it: each line after its number and a tab."""
    return "\n".join(f"{n}\t{line}" for n, line in enumerate(text.split("\n"), 1))


def record(
    prompt: Path, text: str, *, model: str = MODEL,
    calls: list[tuple[str, dict[str, Any]]] | None = None, handback: bool = True,
    first: str | None = None, instructions: list[dict[str, str]] | None = None,
    context: dict[str, Any] | None = None, extra: Iterable[dict[str, Any]] = (),
    read: Callable[[str], str] | None = None, agent_type: str | None = "general-purpose",
    effort: str = "high",
) -> Path:
    """The record of a run briefed to read *prompt* that answered *text*.

    By default the run is briefed as ``rater-brief`` briefs it, replies on
    *model*, reads the whole prompt once and hands *text* back. Each keyword
    changes one thing: the tool *calls* it made, no hand-back, a *first*
    message other than the brief, the *instructions* files or *context* it
    was given, *extra* lines before its answer, what the Read gave it, its
    *agent_type* (none leaves out the file that names it) or its *effort*.
    """
    whole = prompt.read_text() if prompt.exists() else ""
    lines = [
        said(brief(prompt) if first is None else first, promptId="p-1"),
        attached("deferred_tools_delta", addedNames=["Monitor"]),
        said(REMINDER, isMeta=True),
        attached("environment", snapshot={"workingDirectory": FOLDER, "isGitRepo": False}),
        attached("model", identity={"modelId": model}),
        attached("instructions", files=INSTRUCTIONS if instructions is None else instructions),
        attached("session_context", context=context or {}),
        attached("date", date="2026-11-11"),
        attached("credential_org", organizationUuid="00000000-0000-4000-8000-000000000000"),
        attached("prompt_snapshot", systemPrompt=["You are an agent for Claude Code."]),
        {"type": "api-request", "params": {"model": model}},
        replied({"type": "thinking", "thinking": "", "signature": "c2lnbmVk"}, model, effort),
    ]
    for n, (name, given) in enumerate(
        [("Read", {"file_path": str(prompt)})] if calls is None else calls,
    ):
        result = _numbered(whole) if given.get("file_path") == str(prompt) else "…"
        if read is not None:
            result = read(result)
        lines += [replied({"type": "tool_use", "id": f"t{n}", "name": name, "input": given},
                           model, effort),
                  said([{"type": "tool_result", "tool_use_id": f"t{n}", "content": result}])]
    lines += list(extra)
    if handback:
        lines += [
            replied({"type": "tool_use", "id": "h", "name": "SubagentHandback",
                      "input": {"message": text}}, model, effort),
            said([{"type": "tool_result", "tool_use_id": "h",
                   "content": [{"type": "text", "text": '{"success":true}'}]}]),
        ]
    else:
        lines.append(replied({"type": "text", "text": text}, model, effort))
    folder = prompt.parent.parent / "records"
    folder.mkdir(parents=True, exist_ok=True)
    agent = f"a{len(list(folder.glob('agent-*.jsonl'))) + 1:016x}"
    for line in lines:
        if "agentId" in line or line["type"] == "api-request":
            line["agentId"] = agent
    path = folder / f"agent-{agent}.jsonl"
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
    if agent_type is not None:
        path.with_suffix(".meta.json").write_text(json.dumps({
            "agentType": agent_type, "isBuiltIn": agent_type == "general-purpose",
            "launchedWithDefaults": True, "description": "A batch", "spawnDepth": 1,
        }))
    return path


def held_practice(factory: pytest.TempPathFactory) -> Path:
    """A practice run with its blank sheets."""
    root = factory.mktemp("practice") / "run"
    asyncio.run(build_practice(root))
    score(root, materials=load_materials(EXP), prompts=PROMPTS)
    return root


def as_window(held: Path, tmp_path: Path) -> Path:
    """A copy of the practice run *held*, laid out as a scored run's first window."""
    root = tmp_path / "scored"
    window = Path(shutil.copytree(held, root / "window-1"))
    (window / "run.json").unlink()
    (root / "run.json").write_text(json.dumps({"windows": []}))
    return window


def notes_file(tmp_path: Path) -> Path:
    path = tmp_path / "notes.md"
    path.write_text("N1: A problem named only in a heading still counts.\n")
    return path


def out_of(directory: Path, person: str) -> Path:
    return directory.parent / f"batches-{person}"


def batches(directory: Path, notes: Path | None, person: str = "person-1") -> list[Path]:
    return write_batches(directory, person, out_of(directory, person), rubric=RUBRIC, notes=notes)


def sidecar(prompt: Path) -> dict[str, Any]:
    return dict(json.loads(prompt.with_suffix(".json").read_text()))


def given(packet: str, cell: str) -> int | str:
    """A score for every cell: right, or 2."""
    return "right" if cell.startswith("R") else 2


def scores(
    prompt: Path, value: Callable[[str, str], Any] = given,
    edit: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    made = {p: {c: value(p, c) for c in cells} for p, cells in sidecar(prompt)["cells"].items()}
    if edit is not None:
        edit(made)
    return made


def answer(
    prompt: Path, *, model: str = MODEL, value: Callable[[str, str], Any] = given,
    edit: Callable[[dict[str, dict[str, Any]]], None] | None = None, reasons: str = "",
    **kept: Any,
) -> Path:
    """A run that read *prompt* and answered it, as its record."""
    text = (f"model: {model}\n\nEvery packet, with a reason each.{reasons}\n\n"
            f"```json\n{json.dumps(scores(prompt, value, edit))}\n```\n")
    return record(prompt, text, model=model, **kept)


def enter_answer(directory: Path, person: str, prompt: Path, run_record: Path) -> tuple[str, ...]:
    return ai_raters.enter(directory, person, prompt, run_record)


def runs(out: Path) -> list[dict[str, Any]]:
    path = out / RUNS
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def first_memo(made: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return next(cells for cells in made.values() if "C1" in cells)
