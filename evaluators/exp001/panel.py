"""Reads the EXP-001 panel and refuses one the run could not use.

``panel.yaml`` holds the four advisers every arm uses and the wording of every
instruction the harness adds. Arm A shows the advisers' identities to one
model; the channel arms deploy each adviser as a persona agent. Both start
from :func:`adviser_agent_config`, so the two read one source (check 8).

The loader reads what arm A needs: the advisers, its instructions and its
template. It also reads what the channel arms need: the settings the advisers
are deployed with, each arm's channel and memory budget, the operator, and
the memo turn's instructions. An instruction may name another by placeholder,
as ``{memo_format}``. As rubric.yaml's templating note fixes, a placeholder is
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
# A briefing ends when its discussion closes; every other meeting ends with
# the chair's memo turn.
_MEMO_TURN_KEYS = {
    MeetingKind.PLAN: "plan",
    MeetingKind.CONTROL: "plan",
    MeetingKind.RECALL: "recall",
}
# The arms whose advisers meet in a channel, as persona agents.
CHANNEL_ARMS = ("B", "C", "D", "D-prime")
# The persona settings the channel arms deploy exactly as panel.yaml writes
# them; its other persona settings are prose the harness carries out.
_PERSONA_SETTINGS = ("permissions", "relationships", "autonomy", "conversation_window")
# How a channel member may respond, as config/channels.yaml spells it.
_DISPOSITIONS = frozenset(
    {"chair", "participant", "always", "addressed", "when_mentioned", "observer", "never"},
)
_CHANNEL_COUNTS = ("max_rounds", "interaction_budget_tokens", "cascade_depth_cap")
_VOTE_COUNTS = ("end_vote_threshold", "end_vote_window")


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
class ChannelSettings:
    """The channel one channel arm meets in, as panel.yaml sets it.

    ``topic`` still holds its ``{organisation}`` placeholder; ``members``
    maps each adviser to how it responds. The operator is not among them.
    """

    topic: str
    goal: str
    agenda: tuple[str, ...]
    max_rounds: int
    interaction_budget_tokens: int
    escalation_chair_id: str
    convener: str
    classification: str
    cascade_depth_cap: int
    members: Mapping[str, str]
    reasoning_mode: str
    end_vote_threshold: int
    end_vote_window: int


@dataclass(frozen=True, eq=False)
class Panel:
    advisers: tuple[Adviser, ...]
    temperature: float
    memo_format: str
    recall_format: str
    arm_a_template: str
    arm_a_by_meeting: Mapping[str, str]
    persona_settings: Mapping[str, Any]
    operator: str
    channels: Mapping[str, ChannelSettings]
    memory_budget_tokens: Mapping[str, int]
    memo_turn_by_meeting: Mapping[str, str]

    @property
    def chair(self) -> Adviser:
        return next(a for a in self.advisers if a.duty == CHAIR_DUTY)

    def arm_a_instruction(self, kind: MeetingKind) -> str:
        """What arm A is told to write at a meeting of *kind*."""
        return self.arm_a_by_meeting[_ARM_A_KEYS[kind]]

    def memo_turn_instruction(self, kind: MeetingKind) -> str:
        """What the operator asks the chair once a meeting of *kind* has closed."""
        if kind not in _MEMO_TURN_KEYS:
            raise ValueError(f"a {kind.value} has no memo turn")
        return self.memo_turn_by_meeting[_MEMO_TURN_KEYS[kind]]


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
    memo_turn = {
        key: _fill(f"memo_turn.{key}", _at(doc, "instructions", "memo_turn", key), formats)
        for key in sorted(set(_MEMO_TURN_KEYS.values()))
    }
    return Panel(
        advisers=advisers,
        temperature=float(temperature),
        memo_format=formats["memo_format"],
        recall_format=formats["recall_format"],
        arm_a_template=template,
        arm_a_by_meeting=MappingProxyType(by_meeting),
        persona_settings=_persona_settings(doc),
        operator=_operator(doc, advisers),
        channels=MappingProxyType(_channels(doc, advisers)),
        memory_budget_tokens=MappingProxyType(_memory_budgets(doc)),
        memo_turn_by_meeting=MappingProxyType(memo_turn),
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


def _persona_settings(doc: Any) -> Mapping[str, Any]:
    settings = {key: copy.deepcopy(_at(doc, "persona_settings", key)) for key in _PERSONA_SETTINGS}
    window = settings["conversation_window"]
    if isinstance(window, dict):
        window.pop("why", None)  # the reason for the size, for readers; not a setting
    return MappingProxyType(settings)


def _operator(doc: Any, advisers: tuple[Adviser, ...]) -> str:
    oid = _at(doc, "operator", "id")
    # An agent refuses a sender or mention that is not shaped like an ID.
    if not isinstance(oid, str) or not _AGENT_ID.fullmatch(oid):
        raise PanelError(f"operator: {oid!r} is not an agent ID")
    if oid in {a.id for a in advisers}:
        raise PanelError(f"operator {oid} is an adviser")
    respond = _at(doc, "operator", "respond")
    if respond != "observer":
        raise PanelError(f"the operator responds {respond!r}, not as an observer")
    return oid


def _channels(doc: Any, advisers: tuple[Adviser, ...]) -> dict[str, ChannelSettings]:
    """Each channel arm's channel: the shared settings, then its governance block."""
    ids = {a.id for a in advisers}
    chair = next(a.id for a in advisers if a.duty == CHAIR_DUTY)

    def every(key: str) -> Any:
        return _at(doc, "channel", "every_channel_arm", key)

    # An armed channel carries on the discussion the operator's message opens.
    if every("autonomous") is not True:
        raise PanelError(f"every_channel_arm.autonomous is {every('autonomous')!r}, not true")
    if every("escalation_chair_id") != chair:
        raise PanelError(
            f"every_channel_arm.escalation_chair_id is {every('escalation_chair_id')!r}, "
            f"not {chair}, the panel's chair",
        )
    convener = every("convener")
    if convener not in ids or convener == chair:
        raise PanelError(
            f"every_channel_arm.convener is {convener!r}, not an adviser other than the chair",
        )
    counts = {key: _count(f"every_channel_arm.{key}", every(key)) for key in _CHANNEL_COUNTS}
    agenda = every("agenda")
    if not isinstance(agenda, list) or not all(isinstance(i, str) and i.strip() for i in agenda):
        raise PanelError(f"every_channel_arm.agenda is {agenda!r}, not a list of items")
    settings: dict[str, ChannelSettings] = {}
    for name in _at(doc, "channel"):
        if name == "every_channel_arm":
            continue
        members = _at(doc, "channel", name, "members")
        if not isinstance(members, dict) or set(members) != ids:
            raise PanelError(f"{name} members are not the four advisers")
        for aid, respond in members.items():
            if respond not in _DISPOSITIONS:
                raise PanelError(f"{name}: {aid} is {respond!r}, not a disposition")
        votes = {
            key: _count(f"{name}.{key}", _at(doc, "channel", name, key)) for key in _VOTE_COUNTS
        }
        mode = _text(f"{name}.reasoning_mode", _at(doc, "channel", name, "reasoning_mode"))
        channel = ChannelSettings(
            topic=_text("every_channel_arm.topic", every("topic")),
            goal=_text("every_channel_arm.goal", every("goal")),
            agenda=tuple(agenda),
            escalation_chair_id=chair,
            convener=convener,
            classification=_text("every_channel_arm.classification", every("classification")),
            members=MappingProxyType(dict(members)),
            reasoning_mode=mode,
            **counts,
            **votes,
        )
        for arm in _at(doc, "channel", name, "arms"):
            if arm not in CHANNEL_ARMS:
                raise PanelError(f"{name} names arm {arm!r}, which meets in no channel")
            if arm in settings:
                raise PanelError(f"arm {arm} is in two channel blocks")
            settings[arm] = channel
    for arm in CHANNEL_ARMS:
        if arm not in settings:
            raise PanelError(f"no channel block holds arm {arm}")
    return settings


def _memory_budgets(doc: Any) -> dict[str, int]:
    budgets = {}
    for arm in CHANNEL_ARMS:
        tokens = _at(doc, "memory", arm, "memory_budget_tokens")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise PanelError(
                f"memory.{arm}.memory_budget_tokens is {tokens!r}, not a count of tokens",
            )
        budgets[arm] = tokens
    return budgets


def _count(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PanelError(f"{where} is {value!r}, not a positive whole number")
    return value


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
