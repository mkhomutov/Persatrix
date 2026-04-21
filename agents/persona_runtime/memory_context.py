"""Memory context injection for _LLMPersonaAgent.

Handles episodic recall, relationship summary, working-memory
truncation, and note injection into the persona agent's context window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..memory.episodic import (
    _DEFAULT_EPISODIC_MIN_SCORE,
    _DEFAULT_NOTES_MIN_SCORE,
    EpisodicMemory,
)
from ..memory.relationship import RelationshipMemory
from ..memory.working import ContextSection, WorkingMemory, estimate_tokens
from .memory_budget import MemoryBudget, _truncate_to_token_limit

if TYPE_CHECKING:
    # ``from __future__ import annotations`` makes every annotation in this
    # module a string; ``AgentEvent`` is therefore never evaluated at
    # runtime and only needs to be importable for type checkers.  Keeping
    # it inside ``TYPE_CHECKING`` removes the need for the previous
    # TCH001 suppression on the runtime import.
    # (PR #148 review finding L-1: resolve TCH001 suppression.)
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "_MemoryContextMixin",
    "_truncate_with_ellipsis",
    "MemoryInjectionResult",
]


# ─── Constants ─────────────────────────────────────────────

# Total token budget for all memory tiers injected per event.
# RFC 0017 §B / OQ1 resolution: 1500 tokens balances detail vs. prompt size.
# Retune by changing this single constant; no API changes required.
_MEMORY_BUDGET_TOKENS: int = 1500

# Per-call min_tokens floors for the MemoryBudget allocator.
# Each tier specifies the minimum token count a truncated item must have
# to be admitted rather than dropped.  Relationship context uses a higher
# floor (64) because a partially-truncated header line without notes is
# nearly useless; notes use a lower floor (24) to allow even short snippets.
_MIN_TOKENS_RELATIONSHIP: int = 64
_MIN_TOKENS_EPISODIC: int = 32
_MIN_TOKENS_NOTES: int = 24

# Interim per-field cap on ``rel.notes`` (chars).  The pre-RFC-0017 code
# used ``_MAX_RELATIONSHIP_NOTES_CHARS = 300`` as an interim mitigation
# against prompt injection from peer-authored relationship notes.  After
# the allocate-loop rewrite the only remaining bound on the relationship
# block is the per-block budget (~1500 tokens ≈ 6000 chars), which is far
# larger than the original notes cap.  Restore a per-field bound here so
# the prompt-injection surface for ``rel.notes`` does not silently expand
# from ~300 chars to ~6000 chars when the budget order favours this tier.
# 400 chars (~100 tokens at 4 chars/token) matches the original interim
# limit with mild headroom; the TODO(v0.3) note below tracks full
# sanitisation once A2A allows external agents.
# (PR #146 review finding: prompt-injection surface regression.)
_REL_NOTES_INTERIM_CHARS: int = 400

# Per-field char caps applied before the budget loop.  Prevents individual
# items from dominating the prompt even when the token budget is generous.
# Episode summaries beyond 200 chars rarely add recall value and can crowd
# out other tiers.  Note content can be up to 10KB (_MAX_NOTE_CONTENT_BYTES);
# capping at 500 chars keeps injected notes skimmable.
# (PR #146 / F-60-R2-3: word-boundary truncation with ellipsis.)
_MAX_EPISODE_SUMMARY_CHARS: int = 200
_MAX_NOTE_CONTENT_CHARS: int = 500

# Trust score defaults for relationship context filtering.
# A score of exactly _DEFAULT_TRUST_SCORE (the initial value) provides no
# useful signal to the LLM.  Only inject trust when it has deviated by more
# than _TRUST_DEVIATION_THRESHOLD from the default.
# (PR #60 review: unnamed magic numbers in trust comparison.)
_DEFAULT_TRUST_SCORE: float = 0.5
_TRUST_DEVIATION_THRESHOLD: float = 0.01


# ─── Result type ───────────────────────────────────────────


@dataclass(frozen=True)
class MemoryInjectionResult:
    """Return value of :meth:`_MemoryContextMixin._inject_memory_context`.

    Carries per-event allocation metrics so callers can act on the budget
    outcome without coupling to WorkingMemory internals.

    Attributes:
        memory_admitted_tokens: Total tokens admitted across all tiers for
            this event.  Equals ``_MEMORY_BUDGET_TOKENS - budget.remaining``
            after the allocate-loop.  Used by PR 5's empty-context TICK
            short-circuit to decide whether to suppress the LLM call.
    """

    memory_admitted_tokens: int


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
        return _truncate_with_ellipsis_tokens(text, limit)

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


def _truncate_with_ellipsis_tokens(text: str, token_limit: int) -> str:
    """Token-mode implementation for :func:`_truncate_with_ellipsis`.

    Delegates to :func:`~agents.persona_runtime.memory_budget._truncate_to_token_limit`,
    which is the single authoritative implementation for token-boundary truncation.
    Kept as a named helper so :func:`_truncate_with_ellipsis` remains readable.

    PR 1 review finding: the original body was a near-identical copy of
    ``_truncate_to_token_limit`` in ``memory_budget.py``.  Eliminated the
    duplication by delegating here.  ``memory_context`` → ``memory_budget``
    is the unidirectional dependency PR 2 will formalise when wiring
    ``MemoryBudget`` into ``_inject_memory_context``.
    """
    return _truncate_to_token_limit(text, token_limit)


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

    # Stub declaration for method provided by concrete class (via composition).
    if TYPE_CHECKING:
        def _format_event(self, event: AgentEvent) -> str: ...

    async def _inject_memory_context(
        self, event: AgentEvent, *, query: str | None = None,
    ) -> MemoryInjectionResult:
        """Inject episodic, relationship, and note context into working memory.

        Queries the three memory tiers for content relevant to the current
        event and allocates injected tokens via a single :class:`MemoryBudget`
        (RFC 0017 §B).  Tiers are processed in fixed priority order
        (relationship=8, episodic=7, notes=6) so higher-priority tiers
        consume the budget first.  Each item is passed through
        :meth:`MemoryBudget.try_add`; items that exceed the remaining budget
        are truncated or dropped.

        PR 4 (RFC 0017): the TICK skip and ``should_fall_back`` recency-note
        fallback have been removed.  ``recall()`` and ``recall_notes()`` are
        now invoked for every event type; the ``min_score`` thresholds
        (``_DEFAULT_EPISODIC_MIN_SCORE`` / ``_DEFAULT_NOTES_MIN_SCORE``)
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
            equal to ``_MEMORY_BUDGET_TOKENS - budget.remaining`` after the
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
        self._working_memory.remove_section("recent_notes")
        self._working_memory.remove_section("relationship_context")

        # ── Query all three tiers ──────────────────────────────────────────
        # Sequential, not concurrent: all three share the same aiosqlite
        # connection (same db_path).  aiosqlite serialises operations on a
        # single connection, so concurrent gather() would not increase
        # throughput and would add complexity.  If the tiers ever move to
        # separate DB files, this can be revisited.
        # (PR #60 review: document why sequential rather than gather().)

        # Tier 1 (priority 8): Relationship context for the event sender.
        sender_id = event.sender_id
        rel = None
        if sender_id:
            # Extract sender's participant type from event metadata so
            # user relationships are queried correctly.  Without this,
            # get_relationship_summary() defaults to "agent" and silently
            # misses user-type relationships.
            # (PR #120 review F-1: other_participant_type not propagated.)
            # TODO(v0.3): sanitize other_participant_id alongside rel.notes
            # below when A2A allows external agents — the id flows directly
            # into the LLM-visible label and could carry injection content
            # if/when external agents may register arbitrary IDs.
            # (PR #146 re-review: low-risk alignment with rel.notes TODO.)
            sender_type = (
                event.metadata.get("sender_participant_type", "agent")
                if event.metadata
                else "agent"
            )
            try:
                rel = await self._relationship_memory.get_relationship_summary(
                    sender_id,
                    other_participant_type=sender_type,
                )
            except Exception:
                logger.warning(
                    "Agent %s: relationship lookup for %s failed, skipping",
                    self.agent_id, sender_id, exc_info=True,
                )
                rel = None

        # Tier 2 (priority 7): Episodic recall.
        # PR 4: TICK skip removed — the recall-layer min_score threshold
        # filters low-signal TICK content at the DB layer; zero-admission
        # TICK events are handled by the PR 5 empty-context short-circuit.
        # (RFC 0017 §D; previously: PR #60 TICK skip preserved through PR 2/3.)
        try:
            episodes = await self._episodic_memory.recall(
                query,
                limit=5,
                min_score=_DEFAULT_EPISODIC_MIN_SCORE,
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
        try:
            notes = await self._episodic_memory.recall_notes(
                query,
                limit=5,
                min_score=_DEFAULT_NOTES_MIN_SCORE,
            )
        except Exception:
            logger.warning(
                "Agent %s: note recall failed, skipping",
                self.agent_id, exc_info=True,
            )
            notes = []

        # ── Allocate-loop ──────────────────────────────────────────────────
        # Process tiers in fixed priority order (relationship=8 → episodic=7
        # → notes=6).  Higher-priority tiers consume the budget first.
        # RFC 0017 §B / OQ4.
        budget = MemoryBudget(total_tokens=_MEMORY_BUDGET_TOKENS)

        # Relationship tier (priority 8).
        if rel and rel.interaction_count > 0:
            if rel.other_participant_type == "user":
                label = f"{rel.other_participant_id} (Human user)"
            else:
                label = rel.other_participant_id
            rel_lines = [f"Relationship with {label}:"]
            # Only inject trust when it has deviated from the default.
            # A score of exactly _DEFAULT_TRUST_SCORE provides no useful
            # signal to the LLM and implies a measured assessment when
            # it's just the initial value.
            # (F-60-4: skip default trust injection.)
            if abs(rel.trust_score - _DEFAULT_TRUST_SCORE) > _TRUST_DEVIATION_THRESHOLD:
                rel_lines.append(f"  Trust: {rel.trust_score:.2f}")
            rel_lines.append(f"  Interactions: {rel.interaction_count}")
            if rel.notes:
                # TODO(v0.3): sanitize rel.notes when A2A protocol allows
                # external agents — a compromised peer could store prompt
                # injection text in its relationship notes.
                # (PR #60 review: internal prompt injection via peer memory.)
                # Interim per-field char cap retained from pre-RFC-0017
                # code: the per-block budget alone allows ~6000 chars of
                # notes if the relationship tier wins the budget.  Capping
                # here keeps the worst-case injection surface bounded
                # independent of budget allocation order.
                # (PR #146 review.)
                # Use the shared word-boundary + ellipsis helper rather than
                # a raw slice so the LLM-visible truncation marker matches
                # episodic summaries and note content (consistent UX across
                # all three tiers).
                # (PR #146 re-review: truncation-style consistency.)
                capped_notes = _truncate_with_ellipsis(
                    rel.notes, _REL_NOTES_INTERIM_CHARS,
                )
                rel_lines.append(f"  Notes: {capped_notes}")
            rel_text = "\n".join(rel_lines)
            admitted_rel = budget.try_add(rel_text, min_tokens=_MIN_TOKENS_RELATIONSHIP)
            if admitted_rel is not None:
                self._working_memory.add_section(ContextSection(
                    name="relationship_context",
                    # ``token_count`` uses ``estimate_tokens`` (chars/4) so
                    # WorkingMemory's own budget logic stays consistent with
                    # the rest of its sections; the allocate-loop above used
                    # tiktoken via MemoryBudget for the authoritative bound.
                    # The two counts can diverge ~10–20% on prose and 2–3×
                    # on code/JSON/CJK; this is acceptable because the
                    # WorkingMemory budget is a soft secondary cap.
                    # (PR #146 review.)
                    content=admitted_rel,
                    priority=8,
                    token_count=estimate_tokens(admitted_rel),
                    compressible=True,
                ))

        # Episodic tier (priority 7).
        if episodes:
            ep_items: list[str] = []
            for ep in episodes:
                summary = _truncate_with_ellipsis(
                    ep.summary, _MAX_EPISODE_SUMMARY_CHARS,
                )
                admitted = budget.try_add(
                    f"- {summary}", min_tokens=_MIN_TOKENS_EPISODIC,
                )
                if admitted is not None:
                    ep_items.append(admitted)
            if ep_items:
                # NB: the ``"Relevant past episodes:\n"`` header is added
                # AFTER the per-item budget loop and is not itself charged
                # against the budget (~5 tokens of header overhead per
                # non-empty tier).  ``memory_admitted_tokens`` therefore
                # underreports actual injected tokens by a small constant.
                # Acceptable for PR 5's empty-context check (the error
                # direction is safe: zero stays zero); to be revisited if
                # the budget needs to become a hard upper bound.
                # (PR #146 review.)
                text = "Relevant past episodes:\n" + "\n".join(ep_items)
                self._working_memory.add_section(ContextSection(
                    name="episodic_recall",
                    content=text,
                    priority=7,
                    # See relationship tier for the chars/4 vs tiktoken note.
                    token_count=estimate_tokens(text),
                    compressible=True,
                ))

        # Notes tier (priority 6).
        if notes:
            note_items: list[str] = []
            for note in notes:
                content = _truncate_with_ellipsis(
                    note.content, _MAX_NOTE_CONTENT_CHARS,
                )
                admitted = budget.try_add(
                    f"- [{note.topic}] {content}",
                    min_tokens=_MIN_TOKENS_NOTES,
                )
                if admitted is not None:
                    note_items.append(admitted)
            if note_items:
                # See episodic tier above re: untracked header overhead.
                text = "Relevant notes:\n" + "\n".join(note_items)
                self._working_memory.add_section(ContextSection(
                    name="recent_notes",
                    content=text,
                    priority=6,
                    # See relationship tier for the chars/4 vs tiktoken note.
                    token_count=estimate_tokens(text),
                    compressible=True,
                ))

        memory_admitted_tokens = _MEMORY_BUDGET_TOKENS - budget.remaining
        # Consumed by ``_ActionLoopMixin._on_event_inner`` for the RFC 0017
        # §F empty-context TICK short-circuit (PR 5): a zero value, combined
        # with no active goal and no pending turn, suppresses the LLM call
        # on autonomous TICK events.
        return MemoryInjectionResult(memory_admitted_tokens=memory_admitted_tokens)
