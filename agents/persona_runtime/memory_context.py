"""Memory context injection for _LLMPersonaAgent.

Handles episodic recall, relationship summary, working-memory
truncation, and note injection into the persona agent's context window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..memory.episodic import (
    DEFAULT_EPISODIC_MIN_SCORE,
    DEFAULT_NOTES_MIN_SCORE,
    EpisodicMemory,
)
from ..memory.relationship import RelationshipMemory
from ..memory.working import WorkingMemory
from .channel_history import (
    CHANNEL_HISTORY_SECTION_NAME,
    recall_channel_episodes,
    render_channel_history_section,
)
from .channel_roster import inject_channel_roster
from .episodic_section import render_episodic_section
from .facts_section import (
    DEFAULT_FACTS_BUDGET_TOKENS,
    FACTS_SECTION_NAME,
    recall_facts_for_event,
    render_facts_section,
)
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


# ─── Constants ─────────────────────────────────────────────
# Budget totals and per-tier token floors live in memory_budget.py so
# tuning stays co-located with MemoryBudget.try_add() call sites.
# Trust thresholds for the relationship tier live in relationship_section.py
# alongside the rendering logic that consumes them.


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
        # PR 6 — RFC 0017 PR 5 review finding 4: a negative admitted count
        # would silently bypass the ``== 0`` short-circuit in
        # ``_ActionLoopMixin._on_event_inner`` (PR 5).  Refuse to construct
        # in that case so a future ``MemoryBudget`` accounting bug surfaces
        # at the boundary, not as a missed cost-saving opportunity.
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

    In ``"chars"`` mode (default):
        Slices to *limit* chars, cuts at the last space so the LLM sees a
        complete word, and appends ``"..."`` to signal truncation.  If the
        slice contains no space, the full slice is used (3-char overage in
        the worst case, which is acceptable).

    In ``"tokens"`` mode:
        Truncates at a token boundary using tiktoken ``cl100k_base`` when
        available, falling back to char-proportional slicing when tiktoken is
        absent.  The ellipsis ``"\u2026"`` (U+2026) counts toward the token
        budget.  Never panics on missing tiktoken.

    Extracted from _inject_memory_context() where the same pattern was
    copy-pasted for episode summaries, relationship notes, and note content.
    (PR #60 review: truncation pattern duplicated 3 times.)
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

        PR 4 (RFC 0017): the TICK skip and ``should_fall_back`` recency-note
        fallback have been removed.  ``recall()`` and ``recall_notes()`` are
        now invoked for every event type; the ``min_score`` thresholds
        (``DEFAULT_EPISODIC_MIN_SCORE`` / ``DEFAULT_NOTES_MIN_SCORE``)
        applied at the DB layer are the sole filters for low-signal content.
        Zero-admission events (TICK, short greetings) are expected to be
        short-circuited by PR 5's empty-context guard, which consumes the
        ``memory_admitted_tokens`` field on the returned
        :class:`MemoryInjectionResult`.

        Design: each memory tier is wrapped in ``except Exception`` to ensure
        one tier's failure (DB lock, I/O error, corrupted data) never blocks
        event processing.  ``exc_info=True`` logs the full traceback so
        failures are visible to operators.  We intentionally catch broad
        ``Exception`` rather than specific types (OSError, aiosqlite.Error)
        because the memory tier implementations may evolve to raise different
        exception types, and the contract here is "never fail the event".
        ``BaseException`` subclasses (SystemExit, KeyboardInterrupt) are NOT
        caught by ``except Exception``.

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

        # Always remove all three memory sections before (re-)injecting.
        # WorkingMemory.add_section() overwrites a section by name when the
        # tier finds results.  But when a tier finds NO results (e.g. no FTS5
        # matches, or a TICK event that skips episodic recall), add_section()
        # is never called — so a stale section from the previous event silently
        # persists and contaminates the next event's LLM system prompt.
        # Removing unconditionally here makes all three tiers symmetric:
        # section is absent after the call if and only if no results were found.
        # (PR #60 review F-60-R1: stale episodic_recall/recent_notes sections
        # not cleared between events.)
        self._working_memory.remove_section("episodic_recall")
        self._working_memory.remove_section(NOTES_SECTION_NAME)
        self._working_memory.remove_section(RELATIONSHIP_SECTION_NAME)
        self._working_memory.remove_section(CHANNEL_HISTORY_SECTION_NAME)
        self._working_memory.remove_section(FACTS_SECTION_NAME)

        # ── Query all three tiers ──────────────────────────────────────────
        # Sequential, not concurrent: all three share the same aiosqlite
        # connection (same db_path).  aiosqlite serialises operations on a
        # single connection, so concurrent gather() would not increase
        # throughput and would add complexity.  If the tiers ever move to
        # separate DB files, this can be revisited.
        # (PR #60 review: document why sequential rather than gather().)

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
        # subjects the stimulus mentions (RFC 0049 P1; ``stimulus=
        # query`` reuses the formatted event).  Returns ``[]`` when
        # disabled / sender-less / backend raises — all non-fatal.
        if self._facts_enabled:
            facts = await recall_facts_for_event(
                self._fact_store, event, stimulus=query,
            )
        else:
            facts = []

        # Tier 2 (priority 7): Episodic recall.
        # PR 4: TICK skip removed — the recall-layer min_score threshold
        # filters low-signal TICK content at the DB layer; zero-admission
        # TICK events are handled by the PR 5 empty-context short-circuit.
        # (RFC 0017 §D; previously: PR #60 TICK skip preserved through PR 2/3.)
        try:
            # PR 4: ``sessions=None`` = §D default; ``"*"`` pinned unreachable.
            episodes = await self._episodic_memory.recall(
                query,
                limit=5,
                min_score=DEFAULT_EPISODIC_MIN_SCORE,
                sessions=None,
            )
        except Exception:
            logger.warning(
                "Agent %s: episodic recall failed, skipping",
                self.agent_id, exc_info=True,
            )
            episodes = []

        # Tier 3 (priority 6): Recent notes matching event content.
        # Notes recall runs for all event types (including TICK) because notes
        # are agent-authored curated knowledge relevant to autonomous goal review.
        # PR 4: min_score threshold filters low-signal matches at the DB layer;
        # the recency fallback (should_fall_back) is removed because
        # min_score makes "no FTS5 matches" a reliable signal — a threshold-
        # filtered empty result means genuinely no relevant notes, not a
        # missing FTS5 index fallback.  The fallback's recency query would
        # re-admit those low-signal notes, defeating the threshold.
        # (RFC 0017 §D; PR #131 F-1 fallback removed.)
        # Notes tier: room-scoped query recall (§D default). Person identity
        # that follows a person across rooms now lives on the relationship
        # tier (F-7 Option D / PR D3), not a cross-room contact note.
        # Encapsulated in ``notes_section`` like the other tiers' helpers.
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
        # (b)); withhold-only until the PR 6 projection branch.  The
        # relationship tier is deliberately ungated (§C write-through rule
        # + the Non-Goals trust-score carve-out — see ``injection_gate``).
        gate = TurnInjectionGate(
            acting=acting_classification_for_event(event),
            agent_id=self.agent_id,
        )
        channel_episodes = gate.filter_entries("channel_history", channel_episodes)
        episodes = gate.filter_entries("episodic", episodes)
        facts = gate.filter_entries("facts", facts, id_attr="fact_id")
        notes = gate.filter_entries("notes", notes)
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
                # RFC 0026 PR 4 — use-based reinforcement; failure is
                # non-fatal because the section is already staged in
                # ``_working_memory`` and the caller builds the LLM
                # prompt after ``_inject_memory_context`` returns
                # (PR #342 third-pass M-3 — fixes earlier misleading
                # "prompt already shipped" wording).
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
        # Consumed by ``_ActionLoopMixin._on_event_inner`` for the RFC 0017
        # §F empty-context TICK short-circuit (PR 5): a zero value, combined
        # with no active goal and no pending turn, suppresses the LLM call
        # on autonomous TICK events.  The §G manifest labels the admitted
        # subset off the MQ-11 registry (RFC 0037 PR 4 — dark until PR 7).
        return MemoryInjectionResult(
            memory_admitted_tokens=memory_admitted_tokens,
            manifest=gate.manifest(budget),
        )
