"""RFC 0044 Phase 1 — the persona-runtime driver (PR 3).

:class:`PersonaRuntimeDriver` turns a recipe into an observed
:class:`~evaluators.assertions.EvalRun` by driving the **real** persona runtime.
It builds a persona agent around an injected
:class:`~agents.llm_types.LLMProvider` (replay / record / live) and a
:class:`~agents.clock.FrozenClock`, replays each interaction's user turns through
``agent.on_event``, injects each interaction's ``elapsed`` delta into the RFC 0021
temporal seam (OQ #5) via ``clock.advance``, and snapshots the terminal state into
the ``persona:<id>:...`` key space that :func:`~evaluators.eval_set.evaluate`
compares against.

Determinism — the property that lets a golden recorded once replay byte-stably in
CI (RFC 0044 §D) — comes from the ``FrozenClock`` (the only wall-clock the prompt
reads, ``agents/persona_runtime/prompt_assembly.py``) and an in-memory (``:memory:``)
SQLite DB seeded identically each run.

This module imports the ``agents`` runtime, so — like
:mod:`evaluators.replay_llm_client` — it is **not** re-exported from
``evaluators/__init__``: ``import evaluators`` (the pure assertion core) stays free
of the runtime. The runner imports it lazily, only on the CLI / record / drift paths.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evaluators.assertions import EvalRun
from evaluators.eval_set import EvalSet
from evaluators.runner import parse_elapsed

logger = logging.getLogger(__name__)

#: A fixed weekday-afternoon UTC instant (2025-04-25T14:32:00Z) used as the base
#: "now" for every run — matches the RFC 0021 temporal-test epoch so a recorded
#: golden's now-anchor is stable and portable. ``elapsed`` advances from here.
DEFAULT_EPOCH = 1745591520.0

#: Terminal-state key segment that maps onto the relationship trust tier.
_TRUST_PREFIX = "trust.scores."

ConfigResolver = Callable[[str], dict[str, Any]]


def default_config_resolver(config_path: str | Path) -> ConfigResolver:
    """Build a persona-name → config-dict resolver from a ``config/agents.yaml``.

    The file is read once and indexed by ``id``; an unknown persona raises
    :class:`KeyError`, so a recipe naming a persona the config does not declare
    fails loudly rather than driving an empty agent.
    """
    import yaml  # noqa: PLC0415 — deferred so pure importers need not load yaml

    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    agents = data.get("agents") or []
    by_id = {a["id"]: a for a in agents if isinstance(a, dict) and "id" in a}

    def resolve(name: str) -> dict[str, Any]:
        try:
            return by_id[name]
        except KeyError:
            raise KeyError(f"persona {name!r} not found in {config_path}") from None

    return resolve


def _trust_peer(key: str, persona: str) -> str | None:
    """Return the peer id iff ``key`` is ``persona:<persona>:trust.scores.<peer>``.

    The scope and persona segments are validated (not just the trailing
    ``trust.scores.`` marker) so a mis-scoped or wrong-persona key
    (``persona:other:trust.scores.alice``) is *not* silently seeded onto the
    running persona — it falls through to the unsupported-key warning instead.
    """
    parts = key.split(":")
    if (
        len(parts) == 3
        and parts[0] == "persona"
        and parts[1] == persona
        and parts[2].startswith(_TRUST_PREFIX)
    ):
        return parts[2][len(_TRUST_PREFIX):] or None
    return None


def _apply_seed_state(config: dict[str, Any], seed_state: dict[str, Any], *, persona: str) -> None:
    """Translate ``seed_state`` keys into runtime seed inputs on ``config``.

    Phase 1 supports the ``persona:<id>:trust.scores.<peer>`` family: each maps to
    a ``relationships`` entry (``{agent_id, trust_level}``) which
    ``initialize_memory`` seeds as an absolute trust row (``seed_trust``,
    INSERT-OR-IGNORE). Other seed families — and mis-scoped keys — have no runtime
    seam yet, so they are logged and skipped rather than silently dropped: a
    recipe that needs one is visibly unsupported until a later PR wires it.
    """
    if not seed_state:
        return
    relationships = list(config.get("relationships") or [])
    for key, value in seed_state.items():
        peer = _trust_peer(key, persona)
        if peer is None:
            logger.warning(
                "seed_state key %r unsupported in RFC 0044 Phase 1 "
                "(only persona:%s:trust.scores.<peer>) — skipped",
                key,
                persona,
            )
            continue
        relationships.append({"agent_id": peer, "trust_level": value})
    if relationships:
        config["relationships"] = relationships


def _collect_events() -> list[dict[str, Any]]:
    """The flat event stream for this run — empty in Phase 1.

    RFC 0041's typed-event taxonomy is not landed, and there is no capturable
    typed-event stream in the runtime today, so the runner reports no events. The
    assertion engine treats events as opaque ``{"type": ...}`` maps
    (``evaluators/assertions.py``), so ``event_count`` / ``event_sequence`` degrade
    gracefully against the empty list. PR 4+ wires this to the RFC 0041 stream.
    """
    return []


class PersonaRuntimeDriver:
    """Drive a recipe against the real persona runtime → an :class:`EvalRun`."""

    def __init__(
        self,
        *,
        config_resolver: ConfigResolver,
        epoch: float = DEFAULT_EPOCH,
        clock: Any | None = None,
    ) -> None:
        self._resolve = config_resolver
        self._epoch = epoch
        # An injected clock lets a caller observe the elapsed advance directly;
        # left None, each run gets a fresh FrozenClock(epoch) so two runs (record
        # then replay) advance identically from the same base.
        self._clock = clock

    async def run(self, eval_set: EvalSet, provider: Any) -> EvalRun:
        # Deferred runtime imports — this module is only reached on the runner's
        # CLI / record / drift paths, never from `import evaluators`.
        from agents.chat_reply import extract_chat_reply  # noqa: PLC0415
        from agents.clock import FrozenClock  # noqa: PLC0415
        from agents.llm_client import LLMClient  # noqa: PLC0415
        from agents.persona import create_persona_agent  # noqa: PLC0415
        from agents.persona_types import AgentEvent, EventType  # noqa: PLC0415

        setup = eval_set.setup
        config = copy.deepcopy(self._resolve(setup.persona))
        _apply_seed_state(config, setup.seed_state, persona=setup.persona)
        clock = self._clock if self._clock is not None else FrozenClock(self._epoch, tz="UTC")

        agent = create_persona_agent(
            agent_id=setup.persona,
            config=config,
            llm_client=LLMClient(provider),
            clock=clock,
        )

        user = setup.user or "user"
        session = setup.session_id or eval_set.id
        turn_outputs: list[str] = []
        # The persona's reply to the most recent user turn. An `assistant` turn is
        # the *expectation* on that reply, so outputs are appended per assistant
        # turn (not per user turn) — this is the alignment `evaluate` relies on: it
        # checks assistant_turns[idx] against turn_outputs[idx] positionally. Every
        # user turn is still delivered (the conversation advances); only asserted
        # replies occupy an output slot.
        last_reply = ""
        try:
            await agent.initialize_memory()  # inside the try so a partial init still closes
            for interaction in eval_set.interactions:
                if interaction.elapsed:
                    clock.advance(parse_elapsed(interaction.elapsed))
                for turn in interaction.turns:
                    if turn.role == "user":
                        event = AgentEvent(
                            event_type=EventType.CHANNEL_MESSAGE,
                            payload={
                                "content": turn.user_text or "",
                                "user_id": user,
                                "participant_type": "user",
                            },
                            sender_id=user,
                            metadata={
                                "chat_session_id": session,
                                "sender_participant_type": "user",
                            },
                        )
                        actions = await agent.on_event(event)
                        last_reply, _status = extract_chat_reply(actions, user)
                    elif turn.role == "assistant":
                        turn_outputs.append(last_reply)
            terminal_state = await _snapshot_state(agent, setup.persona)
        finally:
            await agent.close_memory()

        return EvalRun(
            turn_outputs=turn_outputs,
            terminal_state=terminal_state,
            events=_collect_events(),
        )


async def _snapshot_state(agent: Any, persona: str) -> dict[str, Any]:
    """Snapshot the persona's terminal state into the ``persona:<id>:...`` space.

    Phase 1 covers the relationship trust tier (the RFC 0044 §A example key
    ``persona:<id>:trust.scores.<peer>``); the ``persona:<id>:...`` flattening is a
    convention the runner owns (no runtime scope uses that prefix). Other tiers are
    added here as recipes come to reference them (PR 4+).
    """
    snapshot: dict[str, Any] = {}
    relationships = await agent.memory.relationship.get_all_relationships()
    for rel in relationships:
        snapshot[f"persona:{persona}:trust.scores.{rel.other_participant_id}"] = rel.trust_score
    return snapshot
