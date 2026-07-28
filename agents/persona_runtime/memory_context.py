"""Memory context injection for _LLMPersonaAgent.

Handles episodic recall, relationship summary, working-memory
truncation, and note injection into the persona agent's context window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..memory._session_filter import SESSIONS_ALL
from ..memory.episodic import (
    DEFAULT_EPISODIC_MIN_SCORE,
    DEFAULT_NOTES_MIN_SCORE,
    EpisodicMemory,
)
from ..memory.episodic_room_ranked import recall_room_ranked
from ..memory.relationship import RelationshipMemory
from ..memory.working import WorkingMemory
from .channel_history import (
    CHANNEL_HISTORY_SECTION_NAME,
    recall_channel_episodes,
    render_channel_history_section,
)
from .channel_roster import inject_channel_roster
from .cross_room import (
    CROSS_ROOM_LIVE,
    DEFAULT_EPISODIC_CROSS_ROOM,
    DEFAULT_FACTS_CROSS_ROOM,
)
from .episodes_shadow import emit_episodes_shadow
from .episodic_section import EPISODIC_RECALL_LIMIT, render_episodic_section
from .facts_section import (
    DEFAULT_FACTS_BUDGET_TOKENS,
    FACTS_SECTION_NAME,
    recall_facts_for_event,
    render_facts_section,
)
from .facts_shadow import emit_facts_shadow
from .injection_gate import (
    InjectionManifestEntry,
    TurnInjectionGate,
    acting_classification_for_event,
)
from .memory_budget import (
    MEMORY_BUDGET_TOKENS,
    MemoryBudget,
    _truncate_to_token_limit,
)
from .notes_section import (
    NOTES_SECTION_NAME,
    recall_notes_for_event,
    render_notes_section,
)
from .projection_branch import apply_episode_projections
from .relationship_section import (
    RELATIONSHIP_SECTION_NAME,
    recall_relationship_summary,
    render_relationship_section,
)

if TYPE_CHECKING:
    # ``from __future__ import annotations`` makes every annotation in this
    # module a string; ``AgentEvent`` is therefore never evaluated at
    # runtime and only needs to be importable for type checkers.  Keeping
    # it inside ``TYPE_CHECKING`` removes the need for the previous
    # TCH001 suppression on the runtime import.
    # (PR #148 review finding L-1: resolve TCH001 suppression.)
    from ..clock import Clock
    from ..memory.facts import FactStore
    from ..persona_types import AgentEvent
    from ..tools.registry import ToolDefinition
    from .channel_roster import ChannelRosterFetcher

logger = logging.getLogger(__name__)

__all__ = [
    "_MemoryContextMixin",
    "_truncate_with_ellipsis",
    "MemoryInjectionResult",
]


# Budget totals / per-tier floors live in memory_budget.py; relationship
# trust thresholds live in relationship_section.py beside their consumers.


# ─── Result type ───────────────────────────────────────────


@dataclass(frozen=True)
class MemoryInjectionResult:
    """Return value of :meth:`_MemoryContextMixin._inject_memory_context`.

    Carries per-event allocation metrics so callers can act on the budget
    outcome without coupling to WorkingMemory internals.

    Attributes:
        memory_admitted_tokens: Total tokens admitted across all tiers for
            this event.  Equals ``MEMORY_BUDGET_TOKENS - budget.remaining``
            after the allocate-loop.  Used by PR 5's empty-context TICK
            short-circuit to decide whether to suppress the LLM call.
        manifest: RFC 0037 §G's per-turn injection manifest (PR 4) — every
            §D-gated, budget-admitted entry as ``(tier, entry_id,
            protection_level)``.  Dark until PR 7 threads it to the
            ``ActionExecutor`` tripwire (which also adds the
            normalized-span hashes); carried here so the turn that
            assembled the prompt owns the record of what reached it.
    """

    memory_admitted_tokens: int
    manifest: tuple[InjectionManifestEntry, ...] = ()

    def __post_init__(self) -> None:
        # RFC 0017 PR 5 review finding 4: a negative admitted count would
        # silently bypass the ``== 0`` short-circuit in ``_on_event_inner``;
        # refuse to construct so an accounting bug surfaces at the boundary.
        if self.memory_admitted_tokens < 0:
            raise ValueError(
                f"memory_admitted_tokens must be >= 0, got {self.memory_admitted_tokens}"
            )


# ─── Helper Functions ──────────────────────────────────────


def _truncate_with_ellipsis(
    text: str,
    limit: int,
    *,
    mode: Literal["chars", "tokens"] = "chars",
) -> str:
    """Truncate *text* to *limit* with word-boundary or token-boundary awareness.

    If *text* fits within *limit* (measured in chars or tokens, depending on
    *mode*), it is returned unchanged.

    In ``"chars"`` mode (default): slices to *limit* chars, cuts at the
    last space so the LLM sees a complete word, and appends ``"..."``;
    a space-free slice is used whole (3-char worst-case overage).

    In ``"tokens"`` mode: truncates at a token boundary via tiktoken
    ``cl100k_base``, falling back to char-proportional slicing when
    tiktoken is absent (never panics).  The ellipsis ``"\u2026"``
    counts toward the token budget.

    Extracted from _inject_memory_context() where the pattern was
    copy-pasted three times.  (PR #60 review.)
    """
    if mode == "tokens":
        # PR 1 review finding 4: ``_truncate_with_ellipsis_tokens`` was a
        # one-liner wrapper around ``_truncate_to_token_limit``.  Inlined
        # here to remove indirection now that the
        # ``memory_context → memory_budget`` import direction is known to
        # be safe (no cycle).
        return _truncate_to_token_limit(text, limit)

    # Original char mode (unchanged).
    if len(text) <= limit:
        return text
    sliced = text[:limit]
    truncated = sliced.rsplit(" ", 1)[0]
    # Zero-space guard: if the slice has no space, rsplit returns it
    # unchanged (len(truncated) == len(sliced)), so we use the full slice.
    if len(truncated) == len(sliced):
        truncated = sliced
    return truncated + "..."


# ─── Mixin ─────────────────────────────────────────────────


class _MemoryContextMixin:
    """Mixin providing memory context injection for _LLMPersonaAgent.

    Expects the following attributes on ``self`` (provided by the
    concrete ``_LLMPersonaAgent`` class and ``PersonaAgent`` base):

    - ``agent_id: str``
    - ``_episodic_memory: EpisodicMemory``
    - ``_relationship_memory: RelationshipMemory``
    - ``_working_memory: WorkingMemory``
    """

    # Attribute declarations for type checkers — set by __init__.
    agent_id: str
    _episodic_memory: EpisodicMemory
    _relationship_memory: RelationshipMemory
    _working_memory: WorkingMemory
    # RFC 0021 PR 2: temporal seam, set by ``_LLMPersonaAgent.__init__``.
    _clock: Clock
    _timezone: str
    # RFC 0026 PR 3 — facts tier seam.  ``_fact_store`` is the per-agent
    # FactStore created by ``create_persona_agent``; ``None`` is the
    # diagnostic-disable path (``memory.facts.enabled: false``) and the
    # back-compat default for legacy mixin harnesses that subclass
    # :class:`_MemoryContextMixin` without wiring a fact store.
    # ``_facts_budget_tokens`` is the per-tier soft slice from
    # ``memory.facts.budget_tokens`` (default
    # :data:`agents.persona_runtime.facts_section.DEFAULT_FACTS_BUDGET_TOKENS`).
    # Class-level defaults keep the older ``test_memory_context_*``
    # harnesses green (assembled without ``create_persona_agent``).
    _fact_store: FactStore | None = None
    _facts_enabled: bool = True
    _facts_budget_tokens: int = DEFAULT_FACTS_BUDGET_TOKENS
    # RFC 0049 PR 2/PR 3 — ``memory.{facts,episodic}.cross_room`` (off|shadow).
    _facts_cross_room: str = DEFAULT_FACTS_CROSS_ROOM
    _episodic_cross_room: str = DEFAULT_EPISODIC_CROSS_ROOM
    # F-4: channel-roster fetcher, wired in ``server_persona`` like the
    # history fetcher. ``None`` (default) → no roster section, so the
    # legacy mixin harnesses and DM-only paths are unaffected.
    _roster_fetcher: ChannelRosterFetcher | None = None
    # Owned by ``_LLMPersonaAgent.__init__`` (set from ``create_memory_tools``)
    # and consumed by ``_ActionLoopMixin``; redeclared here so ``add_recall_tool``
    # type-checks against it.
    _memory_tools: list[ToolDefinition]

    # Stub declaration for method provided by concrete class (via composition).
    if TYPE_CHECKING:
        def _format_event(self, event: AgentEvent) -> str: ...

    def set_roster_fetcher(self, fetcher: ChannelRosterFetcher) -> None:
        """Inject the F-4 channel-roster fetcher (wired in ``server_persona``
        once the shared aiohttp session is open, like the history fetcher)."""
        self._roster_fetcher = fetcher

    def add_recall_tool(self, tool_def: ToolDefinition) -> None:
        """Append the RFC 0036 ``recall_channel_messages`` tool post-construction.

        Wired by :func:`agents.tools.recall.wire_recall_tools` once the shared
        ``aiohttp`` session is open — the agent is built before it exists, so
        the recall tool (which needs the session) is injected here, the same
        post-session shape as :meth:`set_roster_fetcher` /
        :meth:`set_history_fetcher`. It joins ``_memory_tools`` so it is
        surfaced and dispatched per turn alongside the closure-bound memory
        tools (``_build_tool_definitions`` / ``_execute_tools``). Kept in this
        mixin rather than the package ``__init__`` to respect that file's
        zero-headroom size cap (ISSUE-0053).
        """
        self._memory_tools.append(tool_def)

    async def _inject_memory_context(
        self, event: AgentEvent, *, query: str | None = None,
    ) -> MemoryInjectionResult:
        """Inject episodic, relationship, and note context into working memory.

        Queries the memory tiers for content relevant to the current event
        and allocates injected tokens via a single :class:`MemoryBudget`
        (RFC 0017 §B).  Tiers are processed in the canonical cross-RFC
        priority order: relationship → channel history (CHANNEL_MESSAGE
        only) → episodic recall → recent notes; open-commitments and
        duration-priors slots ship empty until v0.4.0.  Pinned verbatim
        by RFC 0011 §E and RFC 0021 §J — see
        :mod:`agents.persona_runtime.channel_history` for the channel
        tier and ``tests/unit/python/test_memory_context_priority_order.py``
        for the order regression guard.  Each item is passed through
        :meth:`MemoryBudget.try_add`; items that exceed the remaining budget
        are truncated or dropped.

        RFC 0017 PR 4: recall runs for every event type; the DB-layer
        ``min_score`` thresholds are the sole low-signal filters, and
        zero-admission events short-circuit via PR 5's empty-context
        guard on the returned ``memory_admitted_tokens``.

        Design: each memory tier is wrapped in a broad ``except
        Exception`` (deliberately not specific types — implementations
        evolve) so one tier's failure never blocks event processing;
        ``exc_info=True`` keeps failures operator-visible.
        ``BaseException`` (SystemExit, KeyboardInterrupt) is not caught.

        Returns:
            :class:`MemoryInjectionResult` with ``memory_admitted_tokens``
            equal to ``MEMORY_BUDGET_TOKENS - budget.remaining`` after the
            allocate-loop.  PR 5 uses this value for the empty-context TICK
            short-circuit.  Callers that ignore the return value are
            unaffected.
        """
        # query is pre-computed by _on_event_inner() to avoid calling
        # _format_event() twice per event.  (F-60-2: deduplicate call.)
        if query is None:
            query = self._format_event(event)

        # Always remove the memory sections before (re-)injecting: a
        # tier with NO results never calls add_section(), so a stale
        # section would silently persist into this event's prompt.
        # Section present iff results found.  (PR #60 review F-60-R1.)
        self._working_memory.remove_section("episodic_recall")
        self._working_memory.remove_section(NOTES_SECTION_NAME)
        self._working_memory.remove_section(RELATIONSHIP_SECTION_NAME)
        self._working_memory.remove_section(CHANNEL_HISTORY_SECTION_NAME)
        self._working_memory.remove_section(FACTS_SECTION_NAME)

        # ── Query all three tiers ──────────────────────────────────────────
        # Sequential, not concurrent (PR #60 review): the tiers share
        # one aiosqlite connection, which serialises operations anyway.

        # Tier 1 (priority 8): Relationship context for the event sender.
        # Recall is delegated to ``relationship_section`` which handles
        # the no-sender / backend-failure cases and metadata-driven
        # participant-type extraction.
        rel = await recall_relationship_summary(
            self._relationship_memory, event, agent_id=self.agent_id,
        )

        # Channel-history tier (RFC 0011 §E + RFC 0021 §J) — issued
        # before the episodic recall so harnesses asserting on
        # ``recall.call_args`` still pin the episodic ``min_score``.
        channel_episodes = await recall_channel_episodes(
            self._episodic_memory, event, agent_id=self.agent_id,
        )

        # Facts tier (RFC 0026 PR 3) — declarative facts about the
        # canonical sender (dementia-test invariant: stored at N,
        # injects at N+1 without subject-string overlap) plus topic
        # subjects ``query`` mentions (RFC 0049 P1).  Returns ``[]``
        # when disabled / sender-less / backend raises — all non-fatal.
        # ``cross_room: live`` (RFC 0049 PR 4, the promoted default)
        # widens the ONE live read past the §D room wall — visibility
        # belongs to the RFC 0037 gate below; shadow mode keeps the
        # walled read and logs the widened delta instead.
        if self._facts_enabled:
            facts = await recall_facts_for_event(
                self._fact_store, event, stimulus=query,
                sessions=SESSIONS_ALL
                if self._facts_cross_room == CROSS_ROOM_LIVE else None,
            )
            await emit_facts_shadow(
                self._fact_store, event, stimulus=query,
                live_fact_ids={f.fact_id for f in facts},
                agent_id=self.agent_id, mode=self._facts_cross_room,
            )
        else:
            facts = []

        # Tier 2 (priority 7): Episodic recall (TICK skip removed —
        # RFC 0017 §D min_score + the PR 5 empty-context short-circuit).
        # ``cross_room: live`` (RFC 0049 PR 4, the promoted default) =
        # room-first-RANKED recall in ONE widened, reinforcing query
        # (the shadow pass does not run in live mode, so the episodic
        # tier costs one read per turn in every mode); otherwise the
        # RFC 0031 §D wall (``sessions=None``; ``"*"`` pinned
        # unreachable) with shadow mode logging the widened delta.
        try:
            if self._episodic_cross_room == CROSS_ROOM_LIVE:
                episodes = await recall_room_ranked(
                    self._episodic_memory, query,
                    limit=EPISODIC_RECALL_LIMIT,
                    min_score=DEFAULT_EPISODIC_MIN_SCORE,
                    reinforce=True,
                )
            else:
                episodes = await self._episodic_memory.recall(
                    query,
                    limit=EPISODIC_RECALL_LIMIT,
                    min_score=DEFAULT_EPISODIC_MIN_SCORE,
                    sessions=None,
                )
        except Exception:
            logger.warning(
                "Agent %s: episodic recall failed, skipping",
                self.agent_id, exc_info=True,
            )
            episodes = []
        await emit_episodes_shadow(
            self._episodic_memory, event, query=query,
            live_episode_ids={e.id for e in episodes},
            agent_id=self.agent_id, mode=self._episodic_cross_room,
        )

        # Tier 3 (priority 6): Recent notes — min_score at the DB layer
        # (RFC 0017 §D / PR #131 F-1).  Room-scoped §D default;
        # cross-room person identity rides the relationship tier (F-7).
        notes = await recall_notes_for_event(
            self._episodic_memory,
            query=query,
            event=event,
            agent_id=self.agent_id,
            min_score=DEFAULT_NOTES_MIN_SCORE,
        )

        # ── RFC 0037 §D hard gate ──────────────────────────────────────────
        # Applied to every channel-derived tier BEFORE the RFC 0017 budget,
        # so a withheld entry never competes for tokens and never reaches
        # the prompt.  The acting level resolves from the trusted event via
        # the positive-list class rule (channel-anchored types read the §B
        # wire stamp; the tick-shaped class floors to ``public`` — §A rule
        # (b)).  The relationship tier is deliberately ungated (§C
        # write-through rule + the Non-Goals trust-score carve-out — see
        # ``injection_gate``).
        gate = TurnInjectionGate(
            acting=acting_classification_for_event(event),
            agent_id=self.agent_id,
        )
        channel_episodes = gate.filter_entries("channel_history", channel_episodes)
        episodes = gate.filter_entries("episodic", episodes)
        facts = gate.filter_entries("facts", facts, id_attr="fact_id")
        notes = gate.filter_entries("notes", notes)
        # §E projection branch (PR 6): a withheld episode with a stored
        # declassified projection ``≤ L`` re-enters as that projection.
        channel_episodes, episodes = await apply_episode_projections(
            gate, self._episodic_memory,
            channel_history=channel_episodes, episodic_entries=episodes,
        )
        gate.emit_log()

        # ── Allocate-loop ──────────────────────────────────────────────────
        # Process tiers in fixed priority order (relationship=8 → episodic=7
        # → notes=6).  Higher-priority tiers consume the budget first.
        # RFC 0017 §B / OQ4.
        budget = MemoryBudget(total_tokens=MEMORY_BUDGET_TOKENS)
        # RFC 0021 PR 2: snapshot the temporal seam once per event.
        now = self._clock.now()

        # Relationship tier (priority 8).  Admission, label formatting,
        # default-trust filtering, and the temporal-recency metric live
        # in ``relationship_section.render_relationship_section``.
        rel_section = render_relationship_section(
            rel, budget,
            now=now, timezone=self._timezone, truncate=_truncate_with_ellipsis,
        )
        if rel_section is not None:
            self._working_memory.add_section(rel_section)

        # Channel-history tier (RFC 0011 §E + RFC 0021 §J).  Slots
        # between relationship and episodic admissions.
        ch_section = render_channel_history_section(
            channel_episodes, budget,
            now=now, timezone=self._timezone, truncate=_truncate_with_ellipsis,
        )
        if ch_section is not None:
            self._working_memory.add_section(ch_section)

        # Facts tier (RFC 0026 PR 3).  Admitted between channel_history
        # and episodic so a high-signal fact displaces lower-signal
        # prose under budget pressure.  Header charged against the
        # global budget; admitted fact_ids land on the per-turn
        # registry for the PR 4 reinforcement write below (MQ-11).
        if facts:
            facts_section = render_facts_section(
                facts, budget,
                facts_budget_tokens=self._facts_budget_tokens,
            )
            if facts_section is not None:
                self._working_memory.add_section(facts_section)
                # RFC 0026 PR 4 — use-based reinforcement; non-fatal
                # because the section is already staged and the caller
                # builds the prompt after this returns (PR #342 M-3).
                admitted_fact_ids = budget.admissions_by_tier("facts")
                if admitted_fact_ids and self._fact_store is not None:
                    try:
                        await self._fact_store.mark_recalled(admitted_fact_ids)
                    except Exception:
                        logger.warning(
                            "Agent %s: fact reinforcement write failed; skipping",
                            self.agent_id, exc_info=True,
                        )

        # Episodic tier (priority 7).  Extracted to
        # ``episodic_section.render_episodic_section`` (F-4 slice B) so the
        # episodic tier matches the other recall tiers' ``render_*`` shape;
        # behaviour (recency tags, budget admission, MQ-11 provenance,
        # ``source="episode"`` metric) is preserved there.
        ep_section = render_episodic_section(
            episodes, budget,
            now=now, timezone=self._timezone, truncate=_truncate_with_ellipsis,
        )
        if ep_section is not None:
            self._working_memory.add_section(ep_section)

        # Notes tier (priority 6).  Extracted to
        # ``notes_section.render_notes_section`` (RFC 0037 PR 4) so the
        # notes tier matches the other tiers' ``render_*`` shape;
        # behaviour (line shape, budget admission, MQ-11 provenance) is
        # preserved there.
        notes_section = render_notes_section(
            notes, budget, truncate=_truncate_with_ellipsis,
        )
        if notes_section is not None:
            self._working_memory.add_section(notes_section)

        # Channel-roster tier (F-4, priority 9 — highest). Group channels
        # only; structural room context injected outside the recall budget
        # (see ``inject_channel_roster``), so it does not affect
        # ``memory_admitted_tokens`` below — group CHANNEL_MESSAGE events are
        # never the TICK that the empty-context short-circuit guards. The
        # helper clears its own stale section (incl. on a later DM turn).
        await inject_channel_roster(
            self._working_memory, self._roster_fetcher, event, self.agent_id,
        )

        memory_admitted_tokens = MEMORY_BUDGET_TOKENS - budget.remaining
        # Consumed by ``_on_event_inner`` for the RFC 0017 §F empty-context
        # TICK short-circuit (PR 5).  The §G manifest labels the admitted
        # subset off the MQ-11 registry (RFC 0037 PR 4 — dark until PR 7).
        return MemoryInjectionResult(
            memory_admitted_tokens=memory_admitted_tokens,
            manifest=gate.manifest(budget),
        )
