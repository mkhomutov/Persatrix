"""The record of one run of an EXP-001 AI rater, checked against the rater rules.

Claude Code keeps a record of every run a session starts: one JSON object
per line, for each message, each tool call and its result, and each thing
Claude Code gives the run besides its brief. An AI rater's run counts only
when its record shows what the rater rules ask
(``docs/experiments/EXP-001-deviation-ai-raters.md``, rules 1 to 3):

- replies from :data:`MODEL` alone, and an answer that names it first;
- the one brief :func:`brief` gives, and no other message to the run;
- no instructions file but the maintainer's own, so no project instructions,
  no memory index and no git status: a rater's session runs from a folder
  of its own, outside any repository;
- one Read of its prompt by its path alone, which gave the whole prompt, and
  no other tool call but the hand-back of its answer.

:func:`checked` returns the answer such a record holds and refuses any
other record. :func:`public` is the part of a record that is published:
the messages and tool calls, without what Claude Code adds around them,
which holds private data. :func:`brief_of` names the prompt a record was
briefed with, so a session's runs can be checked off against its log.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL = "claude-fable-5-2"  # the maintainer's choice for both raters, 2026-11-10
HANDBACK = "SubagentHandback"  # the tool a run hands its answer back with
_BRIEF = (
    "Read the file {prompt} with the Read tool, once, giving it only that path. "
    "Do exactly what the file says. Use no other tool, except SubagentHandback to "
    "hand back your whole reply."
)
_REFUSED = {  # what Claude Code adds to a record when a run is not as briefed
    "queued_command": "a message to the run after its brief",
    "read_truncation_notice": "a prompt cut short",
}
_MODEL_LINE = re.compile(r"\A\s*model:\s*(\S+)\s*$", re.M)
_NUMBERED = re.compile(r"(\d+)\t(.*)", re.S)
_REMINDER = "<system-reminder>"  # how Claude Code's own notes to a run begin


class RaterRefused(ValueError):  # noqa: N818 — named for what the operator sees
    """What the rater's session asked would break the rater rules, so nothing was done."""


@dataclass(frozen=True)
class Run:
    """What a valid run's record shows: its answer, its agent ID in Claude
    Code, and each instructions file it was given, by path, type and hash."""

    answer: str
    agent: str
    instructions: tuple[dict[str, str], ...]


def brief(prompt: Path) -> str:
    """The one message a run of *prompt* is given."""
    return _BRIEF.format(prompt=prompt.resolve())


def entries(record: Path) -> list[dict[str, Any]]:
    """Every line of a run's *record*, read as Claude Code writes it."""
    try:
        text = record.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RaterRefused(f"{record}: the run's record cannot be read ({error})") from None
    found = []
    for line in text.split("\n"):  # never splitlines(): a reply may hold U+2028 raw
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = None
        if not isinstance(entry, dict):
            raise RaterRefused(f"{record}: a line of the run's record cannot be read")
        found.append(entry)
    return found


def checked(record: Path, prompt: Path) -> Run:
    """The answer to *prompt* in a run's *record*, once the record shows the
    run was given nothing but its brief and replied as the rules ask."""
    lines = entries(record)
    said = [e for e in lines if e.get("type") in ("assistant", "user")]
    replies = [e for e in said if e["type"] == "assistant"]
    models = {_message(e).get("model") for e in replies}
    if models != {MODEL}:
        raise RaterRefused(f"the run's record shows replies from "
                           f"{', '.join(sorted(map(str, models))) or 'no model'}, "
                           f"not {MODEL} alone")
    _briefed_only(said, prompt)
    instructions = _given(lines)
    answer = _answer(said, prompt)
    found = _MODEL_LINE.match(answer)
    if found is None:
        raise RaterRefused("the answer does not start with its model line, `model: <id>`")
    if found.group(1) != MODEL:
        raise RaterRefused(f"the run says it ran on {found.group(1)}, not {MODEL}")
    return Run(answer=answer, agent=agent_of(lines) or record.stem, instructions=instructions)


def brief_of(lines: list[dict[str, Any]]) -> str | None:
    """The first message a record shows its run was given."""
    first = next((e for e in lines if e.get("type") == "user"), None)
    return None if first is None else _text(first)


def agent_of(lines: list[dict[str, Any]]) -> str | None:
    """The agent ID Claude Code gave the run, as its record names it."""
    return next((str(e["agentId"]) for e in lines if e.get("agentId")), None)


def public(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The published part of a record: each message and tool call, without
    what Claude Code adds around them, which names the machine, the account,
    the maintainer's own instructions and memory."""
    kept = []
    for e in lines:
        if e.get("type") not in ("assistant", "user"):
            continue
        message = _message(e)
        content = message.get("content")
        kept.append({
            "type": e["type"], "timestamp": e.get("timestamp"),
            **({"isMeta": True} if e.get("isMeta") else {}),
            "message": {
                "role": message.get("role"),
                **({"model": message["model"]} if "model" in message else {}),
                "content": content if isinstance(content, str) else [
                    _public_block(b) for b in content or [] if isinstance(b, dict)
                ],
            },
        })
    return kept


_PUBLISHED = {
    "text": ("text",), "thinking": ("thinking",), "tool_use": ("id", "name", "input"),
    "tool_result": ("tool_use_id", "content"),
}


def _public_block(block: dict[str, Any]) -> dict[str, Any]:
    kind = block.get("type")
    return {"type": kind, **{k: block[k] for k in _PUBLISHED.get(str(kind), ()) if k in block}}


def _briefed_only(said: list[dict[str, Any]], prompt: Path) -> None:
    """Refuse a record whose run was given anything but its brief."""
    if not said or said[0]["type"] != "user" or said[0].get("isMeta") \
            or _text(said[0]) != brief(prompt):
        raise RaterRefused("the run's record does not begin with its brief, as "
                           "`rater-brief` gives it")
    for e in said[1:]:
        if e["type"] != "user":
            continue
        blocks = _blocks(e)
        if blocks and all(b.get("type") == "tool_result" for b in blocks):
            continue
        if e.get("isMeta") and _text(e).startswith(_REMINDER):
            continue  # Claude Code's own note, such as how to hand an answer back
        raise RaterRefused("the run's record shows a message to the run besides its brief")


def _given(lines: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
    """Each instructions file the run was given; refuse a record that shows a
    run given anything that could carry the project into its scores."""
    files = []
    for e in lines:
        attachment = e.get("attachment") if e.get("type") == "attachment" else None
        if not isinstance(attachment, dict):
            continue
        kind = attachment.get("type")
        if kind in _REFUSED:
            raise RaterRefused(f"the run's record shows {_REFUSED[str(kind)]}")
        if kind == "session_context" and (attachment.get("context") or {}).get("gitStatus"):
            raise RaterRefused("the run was given a git status: a rater's session runs from "
                               "a folder of its own, outside any repository")
        if kind != "instructions":
            continue
        for given in attachment.get("files") or []:
            content = str(given.get("content", ""))
            if given.get("type") != "User" and content.strip():
                raise RaterRefused(f"the run was given {given.get('path')}: a rater's run is "
                                   "given no project instructions and no memory index")
            files.append({"path": str(given.get("path")), "type": str(given.get("type")),
                          "sha256": hashlib.sha256(content.encode()).hexdigest()})
    return tuple(files)


def _answer(said: list[dict[str, Any]], prompt: Path) -> str:
    """The answer the run handed back, once its only other tool call is one
    Read of its whole prompt."""
    calls = [b for e in said if e["type"] == "assistant" for b in _blocks(e)
             if b.get("type") == "tool_use"]
    handback = [c for c in calls if c.get("name") == HANDBACK]
    read = [c for c in calls if c.get("name") != HANDBACK]
    if len(read) != 1 or read[0].get("name") != "Read" or len(handback) > 1:
        raise RaterRefused("the run's record shows a tool call other than one Read of its "
                           "prompt and the hand-back of its answer")
    given = read[0].get("input")
    if not isinstance(given, dict) or set(given) != {"file_path"} \
            or Path(str(given["file_path"])).resolve() != prompt.resolve():
        raise RaterRefused("the run's Read was not of its prompt by the prompt's path alone, "
                           "so its one tool call was not the one its brief allows")
    results = [b for e in said if e["type"] == "user" for b in _blocks(e)
               if b.get("type") == "tool_result" and b.get("tool_use_id") == read[0].get("id")]
    if len(results) != 1 or _unnumbered(results[0].get("content")) != prompt.read_text():
        raise RaterRefused("the run's record does not show its Read gave it the whole prompt")
    if handback:
        message = handback[0].get("input", {}).get("message")
        return message if isinstance(message, str) else ""
    last = max(i for i, e in enumerate(said) if e["type"] == "user")
    return "".join(str(b.get("text", "")) for e in said[last + 1:] for b in _blocks(e)
                   if b.get("type") == "text")


def _unnumbered(content: Any) -> str | None:
    """A file's text from what the Read tool gave: each line after its number
    and a tab, counting from 1; None for anything else."""
    if isinstance(content, list):
        content = "".join(str(b.get("text", "")) for b in content
                          if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    lines = []
    for n, line in enumerate(content.split("\n"), 1):
        found = _NUMBERED.fullmatch(line)
        if found is None or int(found.group(1)) != n:
            return None
        lines.append(found.group(2))
    return "\n".join(lines)


def _message(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def _blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    content = _message(entry).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(entry: dict[str, Any]) -> str:
    content = _message(entry).get("content")
    if isinstance(content, str):
        return content
    blocks = _blocks(entry)
    if blocks and all(b.get("type") == "text" for b in blocks):
        return "".join(str(b.get("text", "")) for b in blocks)
    return ""
