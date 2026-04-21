"""Memory context injection for _LLMPersonaAgent.

Handles episodic recall, relationship summary, working-memory
truncation, and note injection into the persona agent's context window.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from ..memory.episodic import EpisodicMemory
from ..memory.relationship import RelationshipMemory
from ..memory.working import ContextSection, WorkingMemory, estimate_tokens
from ..persona_types import AgentEvent, EventType

logger = logging.getLogger(__name__)

__all__ = [
    "_MemoryContextMixin",
    "_truncate_with_ellipsis",
]


# ─── Constants ─────────────────────────────────────────────

# Per-tier truncation caps for memory context injected into working memory.
# build_context() enforces the overall token budget, but truncating per-item
# gives fairer distribution across entries within a tier.  Values balance
# detail vs. budget: notes are longest (agent-authored curated knowledge),
# relationship notes are medium, episode summaries shortest.
# (PR #60 review: inline magic numbers for truncation caps.)
_MAX_EPISODE_SUMMARY_CHARS: int = 200
_MAX_RELATIONSHIP_NOTES_CHARS: int = 300
_MAX_NOTE_CONTENT_CHARS: int = 500

# Trust score defaults for relationship context filtering.
# A score of exactly _DEFAULT_TRUST_SCORE (the initial value) provides no
# useful signal to the LLM.  Only inject trust when it has deviated by more
# than _TRUST_DEVIATION_THRESHOLD from the default.
# (PR #60 review: unnamed magic numbers in trust comparison.)
_DEFAULT_TRUST_SCORE: float = 0.5
_TRUST_DEVIATION_THRESHOLD: float = 0.01


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

    Truncates *text* to fit within *token_limit* tokens (including the
    ellipsis ``\u2026``).  Uses tiktoken when available; falls back to
    char-proportional slicing.  Never panics on missing tiktoken.
    """
    ellipsis = "\u2026"
    if token_limit <= 0:
        return ellipsis

    try:
        import tiktoken  # type: ignore[import-untyped]

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) <= token_limit:
            return text
        ellipsis_tokens = len(enc.encode(ellipsis))
        content_budget = token_limit - ellipsis_tokens
        if content_budget <= 0:
            return ellipsis
        truncated = enc.decode(tokens[:content_budget])
        return truncated + ellipsis
    except ImportError:
        pass
    except Exception:
        logger.warning(
            "tiktoken truncation failed, falling back to char-proportional",
            exc_info=True,
        )

    # Char-proportional fallback: approximate token_limit via chars/4.
    # Reserve 1 token for the ellipsis.
    total_approx = len(text) // 4
    if total_approx <= token_limit:
        return text
    content_budget = token_limit - 1
    if content_budget <= 0:
        return ellipsis
    return text[: content_budget * 4] + ellipsis


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
    ) -> None:
        """Inject episodic, relationship, and note context into working memory.

        Queries the three memory tiers for content relevant to the current
        event and adds them as ``WorkingMemory`` sections with priorities
        that keep them below the system/persona prompts (100/90) but above
        conversation history.

        Priorities: relationship=8, episodic=7, notes=6.
        (F-5b-1: implement deferred memory-context injection.)

        Design: each memory tier is wrapped in ``except Exception`` to ensure
        one tier's failure (DB lock, I/O error, corrupted data) never blocks
        event processing.  ``exc_info=True`` logs the full traceback so
        failures are visible to operators.  We intentionally catch broad
        ``Exception`` rather than specific types (OSError, aiosqlite.Error)
        because the memory tier implementations may evolve to raise different
        exception types, and the contract here is "never fail the event".
        ``BaseException`` subclasses (SystemExit, KeyboardInterrupt) are NOT
        caught by ``except Exception``.
        (PR #60 review: document intent of broad exception handling.)
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
        # The relationship tier had its own remove_section() inside the sender
        # block; that call is now redundant and has been removed.
        # (PR #60 review F-60-R1: stale episodic_recall/recent_notes sections
        # not cleared between events.)
        self._working_memory.remove_section("episodic_recall")
        self._working_memory.remove_section("recent_notes")
        self._working_memory.remove_section("relationship_context")

        # Three memory tiers are queried sequentially rather than concurrently
        # via asyncio.gather() because all three share the same aiosqlite
        # connection (same db_path).  aiosqlite serialises operations on a
        # single connection, so concurrent gather() would not increase
        # throughput and would add complexity.  If the tiers ever move to
        # separate DB files, this can be revisited.
        # (PR #60 review: document why sequential rather than gather().)

        # 1. Episodic recall — recent episodes matching event content.
        # Skip for TICK events: the boilerplate "Autonomous tick: review
        # your goals..." query matches broadly in FTS5, returning
        # low-relevance episodes.  Notes (tier 3) are still injected
        # because the agent's personal knowledge IS relevant for
        # autonomous goal review.
        # (PR #60 review: TICK events waste I/O on low-signal FTS5 matches.)
        if event.event_type == EventType.TICK:
            episodes = []
        else:
            try:
                episodes = await self._episodic_memory.recall(query, limit=5)
            except Exception:
                logger.warning(
                    "Agent %s: episodic recall failed, skipping",
                    self.agent_id, exc_info=True,
                )
                episodes = []

        if episodes:
            lines = ["Relevant past episodes:"]
            for ep in episodes:
                # Cap individual summaries to prevent a single verbose episode
                # from consuming a disproportionate share of the working memory
                # token budget.  build_context() enforces the overall budget, but
                # truncating here gives fairer distribution across episodes.
                # Ellipsis signals truncation to the LLM.  (F-60-R2-3.)
                # (PR #60 review: unbounded episode summary length.)
                summary = _truncate_with_ellipsis(
                    ep.summary, _MAX_EPISODE_SUMMARY_CHARS,
                )
                lines.append(f"- {summary}")
            text = "\n".join(lines)
            self._working_memory.add_section(ContextSection(
                name="episodic_recall",
                content=text,
                priority=7,
                token_count=estimate_tokens(text),
                compressible=True,
            ))

        # 2. Relationship summary for the sender (if present).
        sender_id = event.sender_id
        if sender_id:
            # Extract sender's participant type from event metadata so
            # user relationships are queried correctly.  Without this,
            # get_relationship_summary() defaults to "agent" and silently
            # misses user-type relationships — making the "(Human user)"
            # labeling below dead code.
            # (PR #120 review F-1: other_participant_type not propagated.)
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

            if rel and rel.interaction_count > 0:
                # Label user participants distinctly so the LLM knows
                # whether the sender is a human or another agent.
                if rel.other_participant_type == "user":
                    label = f"{rel.other_participant_id} (Human user)"
                else:
                    label = rel.other_participant_id
                lines = [
                    f"Relationship with {label}:",
                ]
                # Only inject trust when it has deviated from the default.
                # A score of exactly _DEFAULT_TRUST_SCORE provides no useful
                # signal to the LLM and implies a measured assessment when
                # it's just the initial value.
                # (F-60-4: skip default trust injection.)
                if abs(rel.trust_score - _DEFAULT_TRUST_SCORE) > _TRUST_DEVIATION_THRESHOLD:
                    lines.append(f"  Trust: {rel.trust_score:.2f}")
                lines.append(f"  Interactions: {rel.interaction_count}")
                if rel.notes:
                    # TODO(v0.3): sanitize rel.notes when A2A protocol allows
                    # external agents — a compromised peer could store prompt
                    # injection text in its relationship notes.
                    # (PR #60 review: internal prompt injection via peer memory.)
                    # Cap relationship notes to prevent excessive working memory
                    # usage.  No storage cap exists on rel.notes currently.
                    # Ellipsis signals truncation to the LLM.  (F-60-R2-3.)
                    # (F-60-5: unbounded relationship notes in prompt.)
                    rel_notes = _truncate_with_ellipsis(
                        rel.notes, _MAX_RELATIONSHIP_NOTES_CHARS,
                    )
                    lines.append(f"  Notes: {rel_notes}")
                text = "\n".join(lines)
                self._working_memory.add_section(ContextSection(
                    name="relationship_context",
                    content=text,
                    priority=8,
                    token_count=estimate_tokens(text),
                    compressible=True,
                ))

        # 3. Recent notes (top 5 matching event content).
        # Note: for TICK events the query is the same boilerplate
        # "Autonomous tick: review your goals..." string used above.
        # This may return low-signal notes as it does for episodes.
        # Notes recall is preserved on TICK (unlike episodic recall which is
        # skipped) because notes are agent-authored curated knowledge that
        # can be directly relevant to autonomous goal review.  Accepted
        # limitation: low-relevance TICK notes may occasionally be injected.
        # TODO(future): use a different query strategy for TICK notes to
        # improve signal quality (e.g. goal-topic query).
        try:
            notes = await self._episodic_memory.recall_notes(query, limit=5)
        except Exception:
            logger.warning(
                "Agent %s: note recall failed, skipping",
                self.agent_id, exc_info=True,
            )
            notes = []

        # Recency fallback: when FTS5 keyword search found no notes, fall
        # back to the most recent notes so the agent retains awareness of
        # its stored knowledge even when the user's message shares no
        # keywords with note content (e.g. "hi", "remember me?").
        # Reduced limit (3) avoids injecting excessive low-relevance context.
        #
        # Two gates avoid unbounded prompt growth on every event:
        #   1. ``MESSAGE_RECEIVED`` only — TICK / TASK_ASSIGNED queries are
        #      boilerplate ("Autonomous tick: review your goals…") and the
        #      recency notes injected against them are pure noise.
        #   2. Skip when episodic recall already produced results — the
        #      agent already has relevant memory signal; piling on three
        #      arbitrary recent notes adds tokens without adding context.
        # (PR #131 review F-1: unconditional recency-note fallback inflates
        #  every prompt where FTS5 did not match.)
        should_fall_back = (
            not notes
            and event.event_type == EventType.MESSAGE_RECEIVED
            and not episodes
        )
        if should_fall_back:
            try:
                notes = await self._episodic_memory.recall_notes("", limit=3)
            except Exception:
                logger.warning(
                    "Agent %s: recency note fallback failed, skipping",
                    self.agent_id, exc_info=True,
                )
                notes = []

        if notes:
            lines = ["Relevant notes:"]
            for note in notes:
                # Cap note content to prevent disproportionate token usage.
                # Notes can be up to 10KB each (_MAX_NOTE_CONTENT_BYTES);
                # _MAX_NOTE_CONTENT_CHARS balances detail vs budget (longer
                # than episode summaries since notes are user-authored).
                # Ellipsis signals truncation to the LLM.  (F-60-R2-3.)
                # (F-60-1: note content not truncated.)
                content = _truncate_with_ellipsis(
                    note.content, _MAX_NOTE_CONTENT_CHARS,
                )
                lines.append(f"- [{note.topic}] {content}")
            text = "\n".join(lines)
            self._working_memory.add_section(ContextSection(
                name="recent_notes",
                content=text,
                priority=6,
                token_count=estimate_tokens(text),
                compressible=True,
            ))
