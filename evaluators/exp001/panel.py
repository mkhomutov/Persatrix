"""Reads the EXP-001 panel and refuses one the run could not use.

``panel.yaml`` holds the four advisers every arm uses and the wording of every
instruction the harness adds. Arm A shows the advisers' identities to one
model; the channel arms deploy each adviser as a persona agent. Both start
from :func:`adviser_agent_config`, so the two read one source (check 8).

The loader reads what arm A needs: the advisers, its instructions and its
template; the memo turn's instructions and the channel settings are the
channel arms' to read. An instruction may name another by placeholder, as
``{memo_format}``. As rubric.yaml's templating note fixes, a placeholder is
filled by replacing its exact string, in one pass, and every other brace is
literal text; a placeholder a text cannot fill is refused. Trailing blank
space at the end of a text is dropped.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import jsonschema  # type: ignore[import-untyped]
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
# The fields that go under ``persona`` in the agent config; they must meet the
# agent schema's persona definition, so no arm renders a value it would refuse.
_PERSONA = ("title", "background", "behavior", "goals", "knowledge")
_AGENT_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "agent.schema.json"
_TEMPLATE_FIELDS = frozenset({"current_time_line", "organisation", "adviser_identity_blocks"})
# The placeholders rubric.yaml's templating note says the harness fills.
_PLACEHOLDER = re.compile(
    r"\{(memo_format|recall_format|organisation|current_time_line|adviser_identity_blocks)\}",
)
# A control plan is a plan meeting as far as the panel can tell.
_ARM_A_KEYS = {
    MeetingKind.BRIEFING: "briefing",
    MeetingKind.PLAN: "plan",
    MeetingKind.CONTROL: "plan",
    MeetingKind.RECALL: "recall",
}


class PanelError(ValueError):
    """``panel.yaml`` does not have the shape the pre-registration fixes."""


# The mappings below are read-only views, so no caller can change what a later
# meeting reads; eq=False lets an adviser or a panel hash as itself.
@dataclass(frozen=True, eq=False)
class Adviser:
    id: str
    name: str
    duty: str
    title: str
    role: str
    background: str
    behavior: Mapping[str, str]
    goals: Mapping[str, Any]
    knowledge: Mapping[str, Any]


@dataclass(frozen=True, eq=False)
class Panel:
    advisers: tuple[Adviser, ...]
    temperature: float
    memo_format: str
    recall_format: str
    arm_a_template: str
    arm_a_by_meeting: Mapping[str, str]

    @property
    def chair(self) -> Adviser:
        return next(a for a in self.advisers if a.duty == CHAIR_DUTY)

    def arm_a_instruction(self, kind: MeetingKind) -> str:
        """What arm A is told to write at a meeting of *kind*."""
        return self.arm_a_by_meeting[_ARM_A_KEYS[kind]]


def load_panel(path: Path) -> Panel:
    doc = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
    raw_advisers = _at(doc, "advisers")
    if not isinstance(raw_advisers, list):
        raise PanelError(f"advisers is {raw_advisers!r}, not a list")
    advisers = tuple(_adviser(raw) for raw in raw_advisers)
    _check_advisers(advisers)
    formats = {
        key: _text(f"instructions.{key}", _at(doc, "instructions", key))
        for key in ("memo_format", "recall_format")
    }
    by_meeting = {
        key: _fill(
            f"arm_a_by_meeting.{key}", _at(doc, "instructions", "arm_a_by_meeting", key), formats,
        )
        for key in sorted(set(_ARM_A_KEYS.values()))
    }
    if extra := sorted(map(str, _at(doc, "instructions", "arm_a_by_meeting").keys() - by_meeting)):
        raise PanelError(f"arm_a_by_meeting has {', '.join(extra)}, which no meeting reads")
    template = _text("arm_a_system.template", _at(doc, "instructions", "arm_a_system", "template"))
    _check_template(template)
    temperature = _at(doc, "persona_settings", "temperature")
    number = isinstance(temperature, int | float) and not isinstance(temperature, bool)
    if not (number and 0 <= temperature <= 1):
        raise PanelError(
            f"persona_settings.temperature is {temperature!r}, not a number from 0 to 1",
        )
    return Panel(
        advisers=advisers,
        temperature=float(temperature),
        memo_format=formats["memo_format"],
        recall_format=formats["recall_format"],
        arm_a_template=template,
        arm_a_by_meeting=MappingProxyType(by_meeting),
    )


def fill_placeholders(text: str, values: Mapping[str, str]) -> str:
    """*text* with each placeholder replaced by its value, in one pass.

    A value is never scanned again, and every other brace stays as written.
    """
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], text)


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
            "goals": copy.deepcopy(dict(adviser.goals)),
            "knowledge": copy.deepcopy(dict(adviser.knowledge)),
        },
    }


def _adviser(raw: Any) -> Adviser:
    if not isinstance(raw, dict):
        raise PanelError(f"an adviser is {raw!r}, not a mapping")
    aid = raw.get("id")
    # fullmatch, since a bare `$` also passes an ID ending in a newline, which
    # the orchestrator refuses; the agent schema keeps `pool-` for memory pools.
    if not isinstance(aid, str) or not _AGENT_ID.fullmatch(aid) or aid.startswith("pool-"):
        raise PanelError(f"{aid!r} is not an agent ID")
    unknown = sorted(map(str, set(raw) - _KNOWN))
    if unknown:
        raise PanelError(f"{aid}: unknown field {', '.join(unknown)}")
    for field in _REQUIRED:
        if not raw.get(field):
            raise PanelError(f"{aid}: no {field}")
    for field in ("name", "duty", "role"):
        if not isinstance(raw[field], str):
            raise PanelError(f"{aid}: {field} is {raw[field]!r}, not text")
    try:
        jsonschema.validate({key: raw[key] for key in _PERSONA if key in raw}, _persona_schema())
    except jsonschema.ValidationError as exc:
        raise PanelError(f"{aid}: {'.'.join(map(str, exc.absolute_path))}: {exc.message}") from None
    if not any(raw["goals"].get(key) for key in ("primary", "secondary", "hidden")):
        raise PanelError(f"{aid}: goals name no goal")
    return Adviser(
        id=aid,
        name=raw["name"],
        duty=raw["duty"],
        title=raw["title"],
        role=raw["role"],
        background=raw["background"],
        behavior=MappingProxyType(dict(raw["behavior"])),
        goals=MappingProxyType(dict(raw["goals"])),
        knowledge=MappingProxyType(dict(raw.get("knowledge") or {})),
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


@functools.cache
def _persona_schema() -> dict[str, Any]:
    """The agent schema's persona definition, which ``make validate`` holds agents to."""
    schema = json.loads(_AGENT_SCHEMA.read_text())
    return {
        "$schema": schema["$schema"],
        "definitions": schema["definitions"],
        "allOf": [{"$ref": "#/definitions/persona"}],
    }


def _at(doc: Any, *path: str) -> Any:
    """The value at *path* in the panel; one that is not there is refused."""
    value = doc
    for depth, key in enumerate(path, start=1):
        if not isinstance(value, dict) or key not in value:
            raise PanelError(f"panel.yaml has no {'.'.join(path[:depth])}")
        value = value[key]
    return value


def _text(where: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PanelError(f"{where} is {raw!r}, not text")
    return raw.rstrip()


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def _fill(where: str, raw: Any, texts: dict[str, str]) -> str:
    text = _text(where, raw)
    if unfilled := sorted(_placeholders(text) - set(texts)):
        raise PanelError(f"{where} names {_braced(unfilled)}, which it cannot fill")
    return fill_placeholders(text, texts)


def _check_template(template: str) -> None:
    names = _placeholders(template)
    if missing := sorted(_TEMPLATE_FIELDS - names):
        raise PanelError(f"arm_a_system.template lacks {_braced(missing)}")
    if extra := sorted(names - _TEMPLATE_FIELDS):
        raise PanelError(
            f"arm_a_system.template names {_braced(extra)}, which the harness never fills",
        )


def _braced(names: list[str]) -> str:
    return ", ".join(f"{{{name}}}" for name in names)


class _UniqueKeyLoader(yaml.SafeLoader):
    """The safe loader, but a mapping that gives one key twice is refused; the
    plain one would keep the last and say nothing."""


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
    if twice := sorted({key for key in keys if keys.count(key) > 1}):
        raise PanelError(f"line {node.start_mark.line + 1}: {', '.join(twice)} is given twice")
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)
