"""RFC 0031 amendment (F-7 Option D, ISSUE-0093) **PR D2** — rendering the
cross-room person identity into the relationship section.

Two layers:

* a focused unit test on
  :func:`agents.persona_runtime.relationship_section.render_relationship_section`
  — an identity-only relationship (zero interactions) renders, while an
  empty relationship with neither interactions nor identity still
  returns ``None``;
* an integration test of the full write-through → recall → render path
  via :func:`agents.persona.create_persona_agent`, asserting **immediacy**
  (identity surfaces within the first conversation, before any interaction
  is recorded at close) and the **cross-room** property (stated in one
  session, recalled in another) — the two acceptance criteria the
  amendment's test strategy names for D2.
"""

from __future__ import annotations

from agents.memory.relationship_types import RelationshipSummary
from agents.persona import create_persona_agent
from agents.persona_runtime.memory_budget import MemoryBudget
from agents.persona_runtime.memory_context import _truncate_with_ellipsis
from agents.persona_runtime.relationship_section import (
    render_relationship_section,
)
from agents.persona_types import AgentEvent, EventType
from agents.sender_type import sender_type_scope_from_metadata
from agents.session_id import session_scope

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


def _render(rel: RelationshipSummary | None):
    return render_relationship_section(
        rel,
        MemoryBudget(total_tokens=1500),
        now=1_000_000.0,
        timezone="UTC",
        truncate=_truncate_with_ellipsis,
    )


# ─── Render unit ────────────────────────────────────────────


class TestRenderIdentity:
    def test_identity_only_relationship_renders(self):
        """Zero interactions but identity present — the section renders the
        identity line (immediacy: before any interaction is recorded)."""
        rel = RelationshipSummary(
            other_participant_id="user-alice",
            other_participant_type="user",
            trust_score=0.5,
            interaction_count=0,
            last_interaction_at=None,
            notes=None,
            identity={"name": "Alice", "role": "engineer", "prefs": ["Rust"]},
        )
        section = _render(rel)
        assert section is not None
        assert "Identity:" in section.content
        assert "Alice" in section.content
        assert "engineer" in section.content
        assert "Rust" in section.content
        # No interactions → no noisy "Interactions: 0" line.
        assert "Interactions:" not in section.content

    def test_empty_relationship_without_identity_returns_none(self):
        """Regression guard: the pre-D2 early-return still holds when there
        is neither an interaction nor identity."""
        rel = RelationshipSummary(
            other_participant_id="iron-fox",
            other_participant_type="agent",
            trust_score=0.5,
            interaction_count=0,
            last_interaction_at=None,
            notes=None,
            identity=None,
        )
        assert _render(rel) is None

    def test_identity_renders_alongside_interactions(self):
        rel = RelationshipSummary(
            other_participant_id="user-alice",
            other_participant_type="user",
            trust_score=0.5,
            interaction_count=3,
            last_interaction_at=None,
            notes=None,
            identity={"name": "Alice"},
        )
        section = _render(rel)
        assert section is not None
        assert "Identity:" in section.content
        assert "Alice" in section.content
        assert "Interactions: 3" in section.content


# ─── Full path: immediacy + cross-room ──────────────────────


class TestIdentityImmediacyCrossRoom:
    async def test_identity_immediate_and_cross_room(self):
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        store_note = next(
            td for td in agent._memory_tools if td.name == "store_note"
        )
        # Session A ("room-a"): the user states their identity mid-turn and
        # the persona records it via the contact note — write-through fires.
        with session_scope("room-a"), sender_type_scope_from_metadata(
            {"sender_participant_type": "user"},
        ):
            res = await store_note.func(
                topic="contact:user-alice",
                content="Name: Alice. Role: engineer.",
            )
        assert res.success

        # Session B ("room-b"): a different room, and *no* interaction has
        # been recorded (no close).  Identity must still surface — immediacy
        # (before close) and cross-room (different session) together.
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi again"},
            sender_id="user-alice",
            metadata={"sender_participant_type": "user"},
        )
        with session_scope("room-b"):
            await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None, (
            "Identity should surface cross-room from the relationship tier "
            "even with no recorded interaction"
        )
        assert "Alice" in rel_section.content
        assert "engineer" in rel_section.content
        await agent.close_memory()
