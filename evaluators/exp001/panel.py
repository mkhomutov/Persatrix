"""Reads the EXP-001 panel and refuses one the run could not use.

``panel.yaml`` holds the four advisers every arm uses and the wording of every
instruction the harness adds. Arm A shows the advisers' identities to one
model; the channel arms deploy each adviser as a persona agent. Both start
from :func:`adviser_agent_config`, so the two read one source (check 8).

An instruction may name another by placeholder, as ``{memo_format}``; the
loader fills those in and refuses a name it does not know. Trailing blank
space at the end of a text is dropped.
"""

from __future__ import annotations

import copy
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evaluators.exp001.materials import MeetingKind

ADVISERS_PER_PANEL = 4
CHAIR_DUTY = "chair"
_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
_REQUIRED = ("id", "name", "duty", "title", "role", "background", "behavior", "goals")
# Anything else an adviser held, such as quirks, would reach the persona
# agents' prompts but not arm A's, so it is refused. No prompt renders
# knowledge; it only rides into the agent config.
_KNOWN = frozenset((*_REQUIRED, "knowledge"))
_TEMPLATE_FIELDS = frozenset({"current_time_line", "organisation", "adviser_identity_blocks"})
# A control plan is a plan meeting as far as the panel can tell.
_ARM_A_KEYS = {
    MeetingKind.BRIEFING: "briefing",
    MeetingKind.PLAN: "plan",
    MeetingKind.CONTROL: "plan",
    MeetingKind.RECALL: "recall",
}


class PanelError(ValueError):
    """``panel.yaml`` does not have the shape the pre-registration fixes."""


@dataclass(frozen=True)
class Adviser:
    id: str
    name: str
    duty: str
    title: str
    role: str
    background: str
    behavior: dict[str, str]
    goals: dict[str, Any]
    knowledge: dict[str, Any]


@dataclass(frozen=True)
class Panel:
    advisers: tuple[Adviser, ...]
    temperature: float
    memo_format: str
    recall_format: str
    arm_a_template: str
    arm_a_by_meeting: dict[str, str]

    @property
    def chair(self) -> Adviser:
        return next(a for a in self.advisers if a.duty == CHAIR_DUTY)

    def arm_a_instruction(self, kind: MeetingKind) -> str:
        """What arm A is told to write at a meeting of *kind*."""
        return self.arm_a_by_meeting[_ARM_A_KEYS[kind]]


def load_panel(path: Path) -> Panel:
    doc = yaml.safe_load(path.read_text())
    advisers = tuple(_adviser(raw) for raw in doc["advisers"])
    _check_advisers(advisers)
    instructions = doc["instructions"]
    formats = {
        "memo_format": _text(instructions["memo_format"]),
        "recall_format": _text(instructions["recall_format"]),
    }
    by_meeting = {
        key: _fill(f"arm_a_by_meeting.{key}", raw, formats)
        for key, raw in instructions["arm_a_by_meeting"].items()
    }
    missing = sorted(set(_ARM_A_KEYS.values()) - set(by_meeting))
    if missing:
        raise PanelError(f"arm_a_by_meeting has no {', '.join(missing)}")
    template = _text(instructions["arm_a_system"]["template"])
    _check_template(template)
    temperature = doc["persona_settings"]["temperature"]
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        raise PanelError(f"persona_settings.temperature is {temperature!r}, not a number")
    return Panel(
        advisers=advisers,
        temperature=float(temperature),
        memo_format=formats["memo_format"],
        recall_format=formats["recall_format"],
        arm_a_template=template,
        arm_a_by_meeting=by_meeting,
    )


def adviser_agent_config(adviser: Adviser) -> dict[str, Any]:
    """What the persona runtime reads for *adviser*: a fresh copy per call.

    The channel arms add their deployment settings to it; the prompt reads
    only the name and role here and the title, background, behaviour and
    goals under ``persona``.
    """
    return {
        "id": adviser.id,
        "type": "persona",
        "name": adviser.name,
        "role": adviser.role,
        "persona": {
            "title": adviser.title,
            "background": adviser.background,
            "behavior": dict(adviser.behavior),
            "goals": copy.deepcopy(adviser.goals),
            "knowledge": copy.deepcopy(adviser.knowledge),
        },
    }


def _adviser(raw: Any) -> Adviser:
    if not isinstance(raw, dict):
        raise PanelError(f"an adviser is {raw!r}, not a mapping")
    aid = str(raw.get("id"))
    if not _AGENT_ID.match(aid):
        raise PanelError(f"{aid!r} is not an agent ID")
    unknown = sorted(set(raw) - _KNOWN)
    if unknown:
        raise PanelError(f"{aid}: unknown field {', '.join(unknown)}")
    for field in _REQUIRED:
        if not raw.get(field):
            raise PanelError(f"{aid}: no {field}")
    return Adviser(
        id=aid,
        name=str(raw["name"]),
        duty=str(raw["duty"]),
        title=str(raw["title"]),
        role=str(raw["role"]),
        background=str(raw["background"]),
        behavior={str(k): str(v) for k, v in raw["behavior"].items()},
        goals=dict(raw["goals"]),
        knowledge=dict(raw.get("knowledge") or {}),
    )


def _check_advisers(advisers: tuple[Adviser, ...]) -> None:
    if len(advisers) != ADVISERS_PER_PANEL:
        raise PanelError(f"the panel has {len(advisers)} advisers, expected {ADVISERS_PER_PANEL}")
    seen: set[str] = set()
    for adviser in advisers:
        if adviser.id in seen:
            raise PanelError(f"adviser {adviser.id} is listed twice")
        seen.add(adviser.id)
    chairs = sum(a.duty == CHAIR_DUTY for a in advisers)
    if chairs != 1:
        raise PanelError(f"the panel has {chairs} chairs, expected 1")


def _text(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PanelError(f"an instruction is {raw!r}, not text")
    return raw.rstrip()


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def _fill(where: str, raw: Any, texts: dict[str, str]) -> str:
    text = _text(raw)
    unknown = sorted(_fields(text) - set(texts))
    if unknown:
        raise PanelError(f"{where} names {', '.join(map(repr, unknown))}, which is no instruction")
    return text.format_map(texts)


def _check_template(template: str) -> None:
    fields = _fields(template)
    if missing := sorted(_TEMPLATE_FIELDS - fields):
        raise PanelError(f"arm_a_system.template lacks {_braced(missing)}")
    if extra := sorted(fields - _TEMPLATE_FIELDS):
        raise PanelError(
            f"arm_a_system.template names {_braced(extra)}, which the harness never fills",
        )


def _braced(names: list[str]) -> str:
    return ", ".join(f"{{{name}}}" for name in names)
