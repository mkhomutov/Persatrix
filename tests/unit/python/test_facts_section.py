"""Unit tests for :mod:`agents.persona_runtime.facts_section` — the
seed-derivation + section-render contracts that compose with the
declarative-fact tier's reinforcement write.

Two contracts pinned here:

* :func:`_subject_seeds` short-circuit on sender-less events
  (TICK, orchestrator-internal) — the PR 4 cut briefly broke this by
  always seeding ``["self"]`` even when ``event.sender_id`` was empty,
  defeating the PR-5 empty-context cost guard and unconditionally
  issuing ``fact_store.recall(subject="self")`` on every TICK.  The
  PR #342 review M-2 finding reverted the change to gate the
  self-seed on a non-empty sender, preserving MT-MEMORY-005 Leg 5
  (user-facing self-consistency always carries a sender) while
  honoring the sender-less short-circuit.

* :func:`render_facts_section` phantom-reinforcement guard — the PR
  4 cut briefly registered admitted fact_ids on the
  :class:`MemoryBudget` registry inside the per-item loop, *before*
  the per-subject header was admitted; when the header was dropped
  (budget remainder below the truncation floor) the items stayed on
  the registry and the downstream
  :meth:`agents.memory.facts.FactStore.mark_recalled` write fired on
  rows the LLM never saw.  The PR #342 review M-1 finding staged
  the admissions per-block and committed them only after the header
  admitted.

Both classes use direct calls against the module under test rather
than going through ``_inject_memory_context`` — the contracts are
local to ``facts_section.py`` and the unit-level surface is
narrower (no FactStore lifecycle, no full mixin wiring) than the
integration pin in
:mod:`tests.integration.test_facts_reinforcement`.  The integration
counterpart (``TestTickEventDoesNotQueryFactStore``) is intentionally
kept in the integration file so the cost contract is also pinned
end-to-end at the mixin boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ─── _subject_seeds short-circuit (PR #342 review M-2) ─────


class TestSubjectSeedsSenderlessShortCircuit:
    """``_subject_seeds`` returns ``[]`` for events with no
    resolvable sender so the facts tier does not pay a DB round-trip
    on TICK / orchestrator events.

    Background: PR 4 added the ``SELF_SUBJECT`` seed so MT-MEMORY-005
    Leg 5 (self-consistency on user-facing replies) admits ``self.*``
    rows alongside the counterparty rows.  The initial cut always
    seeded ``["self"]`` even when the event had no sender, which:

    * Defeats the PR-5 empty-context short-circuit (TICKs are exactly
      the events that short-circuit is meant to make free).
    * Issues an unconditional ``fact_store.recall(subject="self")``
      on every TICK, which has a cost in the persona's hot path even
      when no self.* facts exist.
    * Risked silently labelling self.* rows under TICKs that nobody
      was supposed to "see" them on.

    Gating the self seed on ``event.sender_id`` preserves Leg 5
    (user-facing legs always carry a sender) while honoring the
    sender-less short-circuit.
    """

    async def test_no_seed_for_none_sender(self) -> None:
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            _subject_seeds,
        )

        event = MagicMock()
        event.sender_id = None
        assert _subject_seeds(event) == []

    async def test_no_seed_for_empty_sender(self) -> None:
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            _subject_seeds,
        )

        event = MagicMock()
        event.sender_id = ""
        assert _subject_seeds(event) == []

    async def test_no_seed_for_whitespace_only_sender(self) -> None:
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            _subject_seeds,
        )

        event = MagicMock()
        event.sender_id = "   "
        assert _subject_seeds(event) == []

    async def test_self_and_sender_both_seeded_when_sender_present(
        self,
    ) -> None:
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            SELF_SUBJECT,
            _subject_seeds,
        )

        event = MagicMock()
        event.sender_id = "bob"
        # ``self`` first so introspective rows survive when the
        # per-tier slice is tight; sender second.
        assert _subject_seeds(event) == [SELF_SUBJECT, "bob"]

    async def test_self_only_when_sender_canonicalises_to_self(
        self,
    ) -> None:
        """Dedup the seed list when sender_id canonicalises to the
        literal ``"self"`` key (defensive — should not happen with the
        validator but the previous code shape allowed it).
        """
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            SELF_SUBJECT,
            _subject_seeds,
        )

        event = MagicMock()
        event.sender_id = "self"
        assert _subject_seeds(event) == [SELF_SUBJECT]


# ─── render_facts_section phantom-reinforcement guard (M-1) ─


class TestNoPhantomReinforcementOnHeaderDrop:
    """Items registered on the admission registry must reflect what
    actually reached the prompt.

    The bug this pins: ``render_facts_section`` charged per-item
    ``budget.try_add`` + ``record_admission`` calls *before* admitting
    the per-subject ``Known facts about <subject>:`` header.  When the
    budget remainder after the items was below the header's
    truncation floor (``MIN_TOKENS_FACTS = 24``), the header was
    dropped and the entire block was ``continue``'d — but the per-
    item registrations had already mutated the registry, and the
    downstream :meth:`FactStore.mark_recalled` write fired on rows
    the LLM never saw.  That contradicted the PR description's
    explicit contract ("the registry is also the source of truth
    the reinforcement write reads off to target only the rows that
    reached the prompt").

    The fix stages admissions per block and registers them on the
    budget only after the header lands.
    """

    async def test_dropped_header_does_not_register_block_admissions(
        self,
    ) -> None:
        """Construct a budget so tight that a short fact line fits
        but the ``Known facts about <subject>:\\n`` header does not,
        and assert the admission registry stays empty.
        """
        from agents.memory.facts import Fact  # noqa: PLC0415 — local
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            render_facts_section,
        )
        from agents.persona_runtime.memory_budget import (  # noqa: PLC0415
            MemoryBudget,
        )

        # 8-token global budget: the fact line "- bob prefers tea"
        # measures 4 tokens under tiktoken cl100k_base (fits whole;
        # remaining = 4) and the header "Known facts about bob:\n"
        # measures 5 tokens (needs truncation to fit in remaining=4;
        # truncated form < MIN_TOKENS_FACTS=24 floor → dropped).
        # Sized via the direct tiktoken probe in the PR #342 review
        # workings — keep the comment in sync if the line format
        # changes.
        budget = MemoryBudget(total_tokens=8)

        facts = [
            Fact(
                fact_id="phantom-fact-1",
                agent_id="dementia-agent",
                subject="bob",
                predicate="prefers",
                object="tea",
                certainty=1.0,
                source_interaction_id="i1",
                asserted_at=1000.0,
                last_recalled_at=None,
                superseded_by=None,
                session_id="legacy",
            ),
        ]

        section = render_facts_section(
            facts, budget, facts_budget_tokens=200,
        )

        # Header didn't admit → block dropped → section must be None.
        assert section is None
        # And critically: no admission may be registered.  If this
        # list is non-empty the reinforcement write would mark
        # ``last_recalled_at`` on a row that never reached the prompt.
        assert budget.admissions_by_tier("facts") == [], (
            "phantom reinforcement: fact_id admitted on registry "
            "despite the per-subject header being dropped — the "
            "reinforcement write would target a row that never "
            "reached the prompt (PR #342 review M-1)."
        )
        # PR #342 third-pass review M-1 — pin the documented soft-overage
        # trade-off.  The fact line admitted into the budget BEFORE the
        # header was tried, so the budget is now down by the item-line
        # cost even though the block was dropped.  ``try_add`` has no
        # rollback seam by design (RFC 0017 §B keeps the greedy
        # allocator stateless across reverts), so downstream tiers
        # (episodic, notes) see the reduced ``budget.remaining``.  This
        # is the deliberate trade-off the ``render_facts_section``
        # docstring documents — pinning the side-effect here so a
        # future peek-then-commit refactor surfaces as a deliberate
        # test update, not silent allocator drift.
        assert budget.remaining < 8, (
            "expected the fact-line token cost to remain debited from "
            "the budget after the dropped-header path; if this passes "
            "with remaining == 8, the allocator has grown a rollback "
            "seam — update the docstring and this pin together"
        )

    async def test_first_subject_block_dropped_does_not_strand_admissions(
        self,
    ) -> None:
        """Multi-subject variant: the first subject's items admit but
        its header drops; the second subject's block (if it admits)
        is the only entry on the registry.

        Pins the more realistic failure mode that PR 4's multi-
        subject fan-out introduces — the first subject's items can
        drain the budget down to a remainder where its own header
        fails to admit while a second subject's block has its own
        independent budget interaction.  The phantom-reinforcement
        guard must scope to the *failing* block, not all blocks.
        """
        from agents.memory.facts import Fact  # noqa: PLC0415 — local
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            render_facts_section,
        )
        from agents.persona_runtime.memory_budget import (  # noqa: PLC0415
            MemoryBudget,
        )

        # 8-token budget: subject "bob"'s item (4 tok) fits; its
        # header (5 tok > remaining=4; truncated < 24 → drops).
        # Subject "self" is also reached but its line cost exceeds
        # the now-near-zero remaining budget so its block is empty
        # anyway — the assertion under test is that bob's admissions
        # do not leak.
        budget = MemoryBudget(total_tokens=8)

        facts = [
            Fact(
                fact_id="bob-fact",
                agent_id="dementia-agent",
                subject="bob",
                predicate="prefers",
                object="tea",
                certainty=1.0,
                source_interaction_id="i1",
                asserted_at=1000.0,
                last_recalled_at=None,
                superseded_by=None,
                session_id="legacy",
            ),
            Fact(
                fact_id="self-fact",
                agent_id="dementia-agent",
                subject="self",
                predicate="self.has_preference",
                object="sci-fi",
                certainty=1.0,
                source_interaction_id="i2",
                asserted_at=1001.0,
                last_recalled_at=None,
                superseded_by=None,
                session_id="legacy",
            ),
        ]

        section = render_facts_section(
            facts, budget, facts_budget_tokens=200,
        )
        # Bob's block dropped (header failed) and self's block could
        # not fit either at the remainder — section is None and the
        # registry is empty.  The fix's contract: a failing block
        # never strands its items on the registry, regardless of
        # whether siblings admit.
        assert section is None
        assert budget.admissions_by_tier("facts") == []


# ─── self-first emit order under tight slice (PR #342 third-pass M-2) ─


class TestFirstSubjectBlockSurvivesUnderTightSlice:
    """Pin the per-block fan-out ordering contract:
    :func:`render_facts_section` admits blocks in
    ``groups.items()`` order (which is caller-order from the
    ``facts`` list).  Under a slice tight enough that only one block
    fits, the FIRST-ordered subject's block must be the survivor.

    Composes with the
    :func:`agents.persona_runtime.facts_section._subject_seeds`
    contract (``self``-first, pinned by
    :class:`TestSubjectSeedsSenderlessShortCircuit.
    test_self_and_sender_both_seeded_when_sender_present`) to give the
    "self survives under budget pressure" guarantee
    :func:`render_facts_section`'s docstring calls out as load-bearing
    for :doc:`MT-MEMORY-005
    <../../../docs/manual-tests/MT-MEMORY-005-dementia-test>` Leg 5.

    Why this is a unit test and not an integration test: the
    integration suite at
    :class:`tests.integration.test_facts_reinforcement.
    TestMultiSubjectSection.test_both_subjects_labelled_in_their_own_block`
    exercises a loose slice where both blocks admit comfortably, so a
    regression flipping the fan-out order leaves it green.  A
    tight-slice integration test would re-test what
    ``_subject_seeds`` + ``render_facts_section`` already pin at the
    unit level, and the integration file is bumping the file_size cap.
    The two unit tests together cover the regression surface:

    * ``_subject_seeds`` returns ``[self, sender]`` (above).
    * ``render_facts_section`` honors caller order under tight slice
      (here).
    """

    async def test_first_block_admits_second_dropped_under_tight_slice(
        self,
    ) -> None:
        """At slice=20 with 7-token lines (verified under tiktoken
        cl100k_base), the first subject's block admits ~2 rows and
        the second subject's block is excluded by the outer-loop
        slice break.  A regression to reversing the iteration order
        would flip the survivor and fail the section-contents
        assertions.

        Keep slice value and per-line token cost in sync if either
        the line format or the inner-loop break condition changes.
        """
        from agents.memory.facts import Fact  # noqa: PLC0415
        from agents.persona_runtime.facts_section import (  # noqa: PLC0415
            render_facts_section,
        )
        from agents.persona_runtime.memory_budget import (  # noqa: PLC0415
            MemoryBudget,
        )

        # Caller-order matches the ``_subject_seeds`` contract:
        # ``self`` rows first, sender rows second.  Each line costs
        # 7 tokens under cl100k_base.
        facts = [
            Fact(
                fact_id="self-1", agent_id="dementia-agent",
                subject="self", predicate="self.has_preference",
                object="sci-fi", certainty=1.0,
                source_interaction_id="i1", asserted_at=1000.0,
                last_recalled_at=None, superseded_by=None,
                session_id="legacy",
            ),
        ]
        for i, pred in enumerate(["prefers", "dislikes", "loves"]):
            facts.append(Fact(
                fact_id=f"bob-{i}", agent_id="dementia-agent",
                subject="bob", predicate=pred,
                object="something something something something",
                certainty=1.0, source_interaction_id=f"i{i+2}",
                asserted_at=1000.0 + i + 1, last_recalled_at=None,
                superseded_by=None, session_id="legacy",
            ))

        budget = MemoryBudget(total_tokens=1500)
        section = render_facts_section(
            facts, budget, facts_budget_tokens=20,
        )

        assert section is not None
        # First-block-survives: the self row reached the prompt;
        # under reversed iteration the bob block would consume the
        # 20-token slice with three 7-token rows and the self block
        # would never be entered.
        assert "self.has_preference" in section.content
        assert "sci-fi" in section.content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
