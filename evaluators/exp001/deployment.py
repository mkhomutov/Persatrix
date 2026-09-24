"""A local deployment of the advisers, for the channel arms' meetings.

Arms B to D-prime hold each meeting in a channel whose members are the four
advisers, run as persona agents, and the operator. A deployment is one
orchestrator process and one process per adviser, all started from one
directory that holds their config, their stores and their logs. B, C and
D-prime get a new directory for every meeting, so each of their meetings
starts with empty stores (check 1).

An adviser is deployed with its panel agent config, unchanged, and the
persona settings panel.yaml lists; a setting it leaves unset takes the value
the shipped reactive personas in ``config/agents.yaml`` use. Every model
alias points at the arms' one model, at the pre-registered prices. The rest
of the orchestrator's config is the shipped config, apart from two changes.
The spending limits are 0, which turns each off (check 6). The REST rate
limiter is off too: the four advisers share its one anonymous bucket, and a
refused publish would drop an adviser's reply without a line in the call
log. Before any process starts, the config the harness writes is checked
against the JSON schemas ``make validate`` uses.

This module writes a deployment's directory; :mod:`evaluators.exp001.processes`
starts and stops its processes.
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agents.validate import validate_config_dir
from evaluators.exp001.costs import ARMS_MODEL, PRICES
from evaluators.exp001.panel import Adviser, Panel, adviser_agent_config, fill_placeholders

REPO = Path(__file__).resolve().parents[2]
SHIPPED_CONFIG = REPO / "config"
SCHEMAS = REPO / "schemas"
# Every adviser answers on this alias; bids and memory summaries take the
# other two, and all three point at the one model.
PERSONA_ALIAS = "quality"
MODEL_ALIASES = ("quality", "fast", "summarizer")
# What the shipped reactive personas set that panel.yaml leaves unset. A
# test holds these to config/agents.yaml.
SHIPPED_PERSONA = {"max_retries": 2, "timeout_seconds": 300}
SHIPPED_AUTONOMY = {"max_actions_per_tick": 1, "idle_after_ticks": 5}
SHIPPED_NOTES = {
    "enabled": True, "max_notes": 500, "auto_reflect_after": 5, "inject_recent_notes": 3,
}
# The shipped spending limits; 0 turns each off.
SPENDING_LIMITS = (
    ("global", "max_daily_usd"),
    ("per_workflow", "default_max_usd"),
    ("per_agent", "default_max_usd"),
)
_COPIED = ("security.yaml", "ui.yaml")


class DeploymentError(RuntimeError):
    """A deployment could not be written, started or stopped as intended."""


@dataclass(frozen=True)
class Alias:
    """What a model alias points at, and the price the orchestrator charges."""

    provider: str
    model: str
    input_per_1m_tokens: float
    output_per_1m_tokens: float

    def price(self) -> dict[str, float]:
        return {
            "input_per_1m_tokens": self.input_per_1m_tokens,
            "output_per_1m_tokens": self.output_per_1m_tokens,
        }


ARMS_ALIAS = Alias(
    "anthropic", ARMS_MODEL, PRICES[ARMS_MODEL].input, PRICES[ARMS_MODEL].output,
)


def adviser_config(panel: Panel, adviser: Adviser, *, memory_db: Path) -> dict[str, Any]:
    """The agents.yaml entry an adviser's process loads.

    It is the adviser's panel config with nothing under ``persona`` added or
    changed, as arm A renders it (check 8), and no reply limit, so the
    persona agents' own applies.
    """
    settings = copy.deepcopy(dict(panel.persona_settings))
    entry = adviser_agent_config(adviser)
    entry.update({
        "model": PERSONA_ALIAS,
        "temperature": panel.temperature,
        "tools": [],  # the built-in note tools remain; no channel recall
        **SHIPPED_PERSONA,
        "permissions": settings["permissions"],
        "autonomy": {**SHIPPED_AUTONOMY, **settings["autonomy"]},
        "memory": {"db_path": str(memory_db), "notes": dict(SHIPPED_NOTES)},
        "conversation_window": settings["conversation_window"],
        "relationships": settings["relationships"],
    })
    return entry


def optimization_config(
    shipped: Mapping[str, Any], *, memory_budget_tokens: int, alias: Alias = ARMS_ALIAS,
) -> dict[str, Any]:
    """The shipped optimization config, with every alias on *alias*, no
    spending limits and the arm's memory budget."""
    config = copy.deepcopy(dict(shipped))
    entry = {"provider": alias.provider, "model": alias.model, **alias.price()}
    config["models"]["aliases"] = {name: dict(entry) for name in MODEL_ALIASES}
    # One row per model the aliases name, as the shipped demo configs derive it.
    config["cost"]["pricing"]["models"] = {alias.model: alias.price()}
    budgets = config["cost"]["budgets"]
    for group, key in SPENDING_LIMITS:
        budgets[group][key] = 0
    config["memory_budget"] = {"tokens": memory_budget_tokens}
    return config


def channel_config(panel: Panel, arm: str, *, name: str, organisation: str) -> dict[str, Any]:
    """One meeting's channel as a channels.yaml entry, shaped like the shipped roundtable."""
    channel = panel.channels[arm]
    members = [{"id": aid, "respond": respond} for aid, respond in channel.members.items()]
    members.append({"id": panel.operator, "respond": "observer"})
    return {
        "name": name,
        "classification": channel.classification,
        "interaction_budget_tokens": channel.interaction_budget_tokens,
        "escalation_chair_id": channel.escalation_chair_id,
        "max_cascade_depth": channel.cascade_depth_cap,
        "end_vote_threshold": channel.end_vote_threshold,
        "end_vote_window": channel.end_vote_window,
        "reasoning": {"mode": channel.reasoning_mode},
        "autonomous": {
            "enabled": True,
            "topic": fill_placeholders(channel.topic, {"organisation": organisation}),
            "agenda": list(channel.agenda),
            "convener": channel.convener,
            "goal": channel.goal,
            "max_rounds": channel.max_rounds,
        },
        "members": members,
    }


def channels_config(
    shipped: Mapping[str, Any], channels: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The shipped fleet settings, declaring *channels* in place of the shipped ones."""
    config = {key: copy.deepcopy(value) for key, value in shipped.items() if key != "channels"}
    config["channels"] = [copy.deepcopy(dict(c)) for c in channels]
    return config


@dataclass(frozen=True)
class Layout:
    """Where one deployment keeps everything it writes."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def memory(self) -> Path:
        return self.root / "memory"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def workflows(self) -> Path:
        return self.root / "workflows"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    def memory_db(self, adviser_id: str) -> Path:
        return self.memory / f"{adviser_id}.db"

    def log(self, process: str) -> Path:
        return self.logs / f"{process}.log"


def write_deployment(
    layout: Layout,
    panel: Panel,
    arm: str,
    *,
    channels: Sequence[Mapping[str, Any]],
    shipped: Path = SHIPPED_CONFIG,
    alias: Alias = ARMS_ALIAS,
) -> None:
    """Write a new deployment's directory, declaring *channels*.

    A directory that already holds anything is refused, since a deployment
    starts with empty stores, and so is config the schemas refuse.
    """
    if layout.root.exists() and any(layout.root.iterdir()):
        raise DeploymentError(f"{layout.root} is not empty; a deployment starts with empty stores")
    for directory in (
        layout.config, layout.data, layout.memory, layout.logs, layout.workflows, layout.workspace,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    agents = [adviser_config(panel, a, memory_db=layout.memory_db(a.id)) for a in panel.advisers]
    optimization = optimization_config(
        _load(shipped / "optimization.yaml"),
        memory_budget_tokens=panel.memory_budget_tokens[arm], alias=alias,
    )
    version = _load(shipped / "agents.yaml")["schema_version"]
    _dump(layout.config / "agents.yaml", {"schema_version": version, "agents": agents})
    _dump(layout.config / "optimization.yaml", optimization)
    declared = channels_config(_load(shipped / "channels.yaml"), channels)
    _dump(layout.config / "channels.yaml", declared)
    for name in _COPIED:
        shutil.copyfile(shipped / name, layout.config / name)
    ok, errors, _ = validate_config_dir(
        str(layout.config), str(SCHEMAS), workflow_dir=str(layout.workflows),
    )
    if not ok:
        raise DeploymentError("the deployment's config fails its schemas:\n" + "\n".join(
            str(e) for e in errors
        ))


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def _dump(path: Path, doc: Any) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
