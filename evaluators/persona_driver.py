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

RFC 0034 working memory is opt-in per recipe. A recipe that declares
``setup.channel`` gets an :class:`~evaluators.eval_channel_history.InProcessChannelHistory`
wired as its conversation-window history fetcher, and each delivered turn is logged
to it — so the persona's window reconstructs the in-channel transcript and it sees
its own prior turns. A recipe with no channel drives the pre-window
current-event-only path, byte-identical to before, so enabling this never perturbs
a landed channel-less golden (e.g. EVAL-MEMORY-001).

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from evaluators.assertions import EvalRun
from evaluators.eval_channel_history import InProcessChannelHistory
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


def _force_in_memory_db(config: dict[str, Any]) -> None:
    """Pin the persona's memory to an isolated ``:memory:`` SQLite DB.

    Overrides whatever ``memory.db_path`` the resolved config carries (a
    production persona points at a real file, e.g. ``data/memory.db``). Two
    invariants ride on this — the module docstring's determinism contract:

    * **Golden portability.** A recorded golden must replay byte-stably on a
      *fresh* clone. A persona's file DB carries ambient rows from prior runs
      that would shift the recalled prompt at record vs. replay time → a
      cassette miss. ``:memory:`` starts empty every run, so record and replay
      populate identical memory from the recipe's turns alone.
    * **No production pollution.** An eval must never write into a persona's
      real memory DB.

    Only ``db_path`` is overridden; the rest of the ``memory`` block (notes /
    facts / working budgets) is preserved because it shapes prompt assembly and
    so is part of what the golden pins.
    """
    memory_cfg = config.get("memory")
    if not isinstance(memory_cfg, dict):
        memory_cfg = {}
        config["memory"] = memory_cfg
    memory_cfg["db_path"] = ":memory:"


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


class _ShadowTraceHandler(logging.Handler):
    """Collects the structured payload off each shadow log record.

    ``traces`` may be shared across handlers (RFC 0049 PR 3: one handler
    per shadow logger, one merged stream) — records append in emission
    order, so a run's L2 (facts) and L1 (episodes) traces interleave
    chronologically; consumers partition on each payload's ``tier`` key.
    """

    def __init__(self, attr: str, traces: list[dict[str, Any]]) -> None:
        super().__init__(level=logging.INFO)
        self._attr = attr
        self.traces = traces

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, self._attr, None)
        if isinstance(payload, dict):
            self.traces.append(payload)


@contextmanager
def capture_shadow_traces() -> Iterator[list[dict[str, Any]]]:
    """Capture RFC 0049 cross-room shadow traces for the block's duration.

    Attaches a collecting handler directly to each of the runtime's
    shadow loggers — ``agents.persona_runtime.facts_shadow`` (L2, PR 2)
    and ``agents.persona_runtime.episodes_shadow`` (L1, PR 3) — and,
    because a handler only sees records the logger's effective level
    admits, temporarily lowers each to ``INFO`` when the ambient config
    would filter the trace. Both are restored on exit, so the harness
    never perturbs the process's logging outside the run. Shadow traces
    are the measurement input for the RFC 0049 PR 4 shadow→live
    promotion gate; the runner threads the captured (merged, ``tier``-
    keyed) list into the report artifact.

    The runtime imports are deferred (module convention: ``agents``
    loads only on the driver paths, never from ``import evaluators``).
    """
    from agents.persona_runtime import (  # noqa: PLC0415
        episodes_shadow,
        facts_shadow,
    )

    traces: list[dict[str, Any]] = []
    attached: list[tuple[logging.Logger, logging.Handler, int]] = []
    try:
        for mod in (facts_shadow, episodes_shadow):
            shadow_logger = logging.getLogger(mod.SHADOW_LOGGER_NAME)
            handler = _ShadowTraceHandler(mod.SHADOW_TRACE_ATTR, traces)
            prev_level = shadow_logger.level
            shadow_logger.addHandler(handler)
            if shadow_logger.getEffectiveLevel() > logging.INFO:
                shadow_logger.setLevel(logging.INFO)
            attached.append((shadow_logger, handler, prev_level))
        yield traces
    finally:
        for attached_logger, attached_handler, level in attached:
            attached_logger.removeHandler(attached_handler)
            attached_logger.setLevel(level)


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
        _force_in_memory_db(config)
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
        # RFC 0034 working memory (opt-in per recipe via ``setup.channel``). With a
        # channel declared, an in-process history fetcher is wired and every
        # delivered turn is logged, so the persona's conversation window
        # reconstructs the in-channel transcript — the persona sees its own prior
        # turns (``agents/persona_runtime/conversation_window.py``). Without a
        # channel the driver stays on the pre-window current-event-only path,
        # byte-identical to a channel-less recipe (e.g. EVAL-MEMORY-001), so this
        # is purely additive and never perturbs a landed golden. Message ids are a
        # deterministic monotonic sequence so the window content — and thus every
        # request hash — is stable across a record and its replays (RFC 0044 §D).
        channel = setup.channel
        history: InProcessChannelHistory | None = None
        if channel:
            history = InProcessChannelHistory()
            agent.set_history_fetcher(history)
        msg_seq = 0
        turn_outputs: list[str] = []
        # The persona's reply to the most recent user turn. An `assistant` turn is
        # the *expectation* on that reply, so outputs are appended per assistant
        # turn (not per user turn) — this is the alignment `evaluate` relies on: it
        # checks assistant_turns[idx] against turn_outputs[idx] positionally. Every
        # user turn is still delivered (the conversation advances); only asserted
        # replies occupy an output slot.
        last_reply = ""
        # RFC 0049 PR 2/PR 3 — capture the runtime's cross-room shadow logs
        # (L2 facts + L1 episodes, ``tier``-keyed) for the run: the traces
        # ride the EvalRun (and the report artifact) as the PR 4 measurement
        # input. Single-room recipes capture [] — the shadow passes emit
        # nothing when the cross-room delta is empty.
        shadow_traces: list[dict[str, Any]] = []
        try:
            await agent.initialize_memory()  # inside the try so a partial init still closes
            with capture_shadow_traces() as shadow_traces:
                for interaction in eval_set.interactions:
                    if interaction.elapsed:
                        clock.advance(parse_elapsed(interaction.elapsed))
                    for turn in interaction.turns:
                        if turn.role == "user":
                            # Log the inbound turn *before* dispatch (as the
                            # orchestrator persists an inbound message before the
                            # persona acts) so it is the ordering anchor the window
                            # dedups the current event against.
                            message_id: str | None = None
                            if history is not None and channel:
                                message_id = f"m{msg_seq}"
                                msg_seq += 1
                                history.append(
                                    channel_id=channel,
                                    message_id=message_id,
                                    sender_id=user,
                                    content=turn.user_text or "",
                                )
                            event = AgentEvent(
                                event_type=EventType.CHANNEL_MESSAGE,
                                payload={
                                    "content": turn.user_text or "",
                                    "user_id": user,
                                    "participant_type": "user",
                                },
                                sender_id=user,
                                channel_id=channel,
                                message_id=message_id,
                                metadata={
                                    "chat_session_id": session,
                                    "sender_participant_type": "user",
                                    # RFC 0037 §B (PR 4): mirror the orchestrator's
                                    # dispatch-time classification stamp (DM default
                                    # ``internal``) — without it the turn floors to
                                    # the §D ``public`` acting level (rule (b)) and
                                    # every internal-stamped memory is withheld,
                                    # which is the version-skew posture, not the
                                    # production wire shape this driver replays.
                                    "channel_classification": "internal",
                                },
                            )
                            actions = await agent.on_event(event)
                            last_reply, _status = extract_chat_reply(actions, user)
                            # Log the persona's reply *after* — so the window replays
                            # it as the persona's own prior ``assistant`` turn next time.
                            if history is not None and channel:
                                history.append(
                                    channel_id=channel,
                                    message_id=f"m{msg_seq}",
                                    sender_id=setup.persona,
                                    content=last_reply,
                                )
                                msg_seq += 1
                        elif turn.role == "assistant":
                            turn_outputs.append(last_reply)
            terminal_state = await _snapshot_state(agent, setup.persona)
        finally:
            await agent.close_memory()

        return EvalRun(
            turn_outputs=turn_outputs,
            terminal_state=terminal_state,
            events=_collect_events(),
            shadow_traces=list(shadow_traces),
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
