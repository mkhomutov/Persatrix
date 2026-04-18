"""
Agent loading and persona lifecycle helpers for AgentServer.

- ``_resolve_agent_type`` / ``load_agent`` — agent config parsing and
  instantiation (task and persona agents).
- ``initialize_persona_agents`` — persona memory, dispatcher, and tick
  scheduler startup, called from ``AgentServer.start()``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .base import BaseAgent
from .llm_client import LLMClient, create_provider
from .persona import create_persona_agent
from .persona_runtime import _LLMPersonaAgent
from .task_agent import TaskAgent
from .tick import TickScheduler
from .tools import builtin
from .tools.permissions import PermissionGate
from .tools.sandbox import PathValidator

if TYPE_CHECKING:
    from .dispatch import EventDispatcher

logger = logging.getLogger("Persatrix.agent.server_persona")

# _resolve_agent_type is intentionally excluded: it is a private helper
# used only within load_agent; tests that import it directly are accessing
# an implementation detail, not part of this module's public contract.
__all__ = ["load_agent", "initialize_persona_agents"]

# Agent IDs must match the cross-component contract shared with the Go
# orchestrator registry.  Validated at load time to prevent routing mismatches.
_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


# ─── Agent type resolution ───────────────────────────────────


def _resolve_agent_type(agent_config: dict[str, Any]) -> str:
    """Resolve agent type string from the ``type`` field in agent config.

    Supported types:
    - ``task`` (default) — data-driven TaskAgent with YAML instructions
    - ``persona`` — LLM-powered PersonaAgent with memory and autonomy

    Agents without a ``type`` field default to ``task`` for backward
    compatibility with v0.1 configs.
    """
    agent_type = agent_config.get("type", "task")

    match agent_type:
        case "task":
            return "task"
        case "persona":
            return "persona"
        case _:
            raise SystemExit(
                f"Unknown agent type {agent_type!r} for agent "
                f"{agent_config['id']!r}. Supported types: task, persona"
            )


# ─── Agent loading ───────────────────────────────────────────


def load_agent(agent_id: str, config_path: str, workspace: str) -> BaseAgent:
    """Load an agent by ID from YAML config.

    Returns a fully-initialized BaseAgent with LLM client, tools, and
    permission configuration wired.
    """
    if not _AGENT_ID_PATTERN.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID {agent_id!r}: "
            f"must match {_AGENT_ID_PATTERN.pattern}"
        )

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(f"Agent config not found: {config_path}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}")

    # A malformed config like ``agents: "string"`` would otherwise fail
    # with an unclear TypeError during enumeration.
    agents_list = config.get("agents", [])
    if not isinstance(agents_list, list):
        raise SystemExit(
            f"'agents' key in {config_path} must be a list, "
            f"got {type(agents_list).__name__}"
        )

    # Validate 'id' field presence before the dict comprehension to
    # surface a clear SystemExit instead of a raw KeyError.
    for i, a in enumerate(agents_list):
        if "id" not in a:
            raise SystemExit(
                f"Agent config entry {i} missing required 'id' field"
            )
    # Dict comprehension silently takes the last duplicate ID;
    # detect and reject duplicates before building the map.
    seen_ids: set[str] = set()
    for a in agents_list:
        aid = a["id"]
        if aid in seen_ids:
            raise SystemExit(
                f"Duplicate agent ID {aid!r} in {config_path}"
            )
        seen_ids.add(aid)
    agent_configs = {a["id"]: a for a in agents_list}
    if agent_id not in agent_configs:
        raise SystemExit(f"Agent {agent_id!r} not found in {config_path}")

    agent_config = agent_configs[agent_id]
    agent_type = _resolve_agent_type(agent_config)

    # Validate required 'model' field at startup so operators see a
    # clear message instead of a raw KeyError from create_provider().
    if "model" not in agent_config:
        raise SystemExit(
            f"Agent {agent_id!r} missing required 'model' field in config"
        )

    # Create LLM client
    provider = create_provider(agent_config)
    llm_client = LLMClient(provider)

    # Create agent based on type
    agent: BaseAgent
    if agent_type == "persona":
        agent = create_persona_agent(
            agent_id=agent_id,
            config=agent_config,
            llm_client=llm_client,
        )
    else:
        agent = TaskAgent(
            agent_id=agent_id,
            config=agent_config,
            llm_client=llm_client,
        )

    # Wire built-in tool dependencies
    permissions = agent_config.get("permissions", {})
    builtin.permission_gate = PermissionGate(permissions)
    fs = permissions.get("filesystem", {})
    builtin.path_validator = PathValidator(
        allow_read=fs.get("read", []),
        allow_write=fs.get("write", []),
        deny=fs.get("deny", []),
    )
    builtin.workspace_root = Path(workspace).resolve()

    return agent


# ─── Persona lifecycle ───────────────────────────────────────


async def initialize_persona_agents(
    agents: dict[str, BaseAgent],
    dispatcher: EventDispatcher,
    tick_schedulers: dict[str, TickScheduler],
) -> None:
    """Initialize memory, dispatcher, and tick schedulers for persona agents.

    Called from ``AgentServer.start()`` after the gRPC server is listening,
    so memory failures cannot prevent non-persona agents from serving.

    Memory failure for a given agent is logged and that agent is skipped
    (it will NOT receive dispatched events or tick scheduling).

    ``tick_schedulers`` is mutated in-place: started TickScheduler instances
    are inserted under their agent ID.  Non-persona agents are left untouched.
    """
    for agent_id, agent in agents.items():
        if not isinstance(agent, _LLMPersonaAgent):
            continue

        # Memory initialization — must succeed before dispatch/tick.
        try:
            await agent.initialize_memory()
            logger.info("Initialized memory for persona agent %s", agent_id)
        except Exception:
            logger.exception(
                "Failed to initialize memory for agent %s — "
                "agent will NOT receive dispatched events or tick scheduling",
                agent_id,
            )
            continue

        # Register with event dispatcher.
        dispatcher.register_agent(agent_id, agent)

        # Start tick scheduler for autonomous agents.
        autonomy = agent.config.get("autonomy", {})
        level = autonomy.get("level", "reactive")
        if level in ("semi-autonomous", "autonomous"):
            interval = autonomy.get("tick_interval_seconds", 60)
            max_actions = autonomy.get("max_actions_per_tick", 3)
            idle_after = autonomy.get("idle_after_ticks", 10)
            scheduler = TickScheduler(
                agent,
                interval=float(interval),
                max_actions_per_tick=max_actions,
                idle_after_ticks=idle_after,
                executor=dispatcher.executor,
            )
            tick_schedulers[agent_id] = scheduler
            dispatcher.register_tick_scheduler(agent_id, scheduler)
            scheduler.start()
            logger.info(
                "Started tick scheduler for %s (interval=%ds)",
                agent_id,
                interval,
            )
