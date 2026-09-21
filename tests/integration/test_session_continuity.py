"""RFC 0031 Phase 2 PR 5 — dementia-test bridge: F-3 fix proven compatible
with long-arc continuity.

The canonical regression bar that closes Phase 2.  This file pins both
halves of `RFC 0031 §D
<../../docs/rfcs/0031-per-session-namespacing-channels.md#d-recall-semantics>`_'s
goal:

* **Isolation by default** — a second run under a new
  ``PERSATRIX_SESSION_ID`` does not surface the prior run's persona
  memory through a tier's default ``sessions=None`` recall (the F-3 closer).
* **Continuity within a session** — a multi-event arc that shares a
  session id reads back the full arc from every tier (the dementia-test
  bridge — `OQ #1 resolution 1a
  <../../docs/rfcs/0031-per-session-namespacing-channels.md#open-questions>`_:
  default single-session recall **is** the dementia-test recall path).
* **Cross-session continuity by opt-in** — an explicit
  ``sessions=[arc1, arc2]`` reads across both arcs (the bridge for
  long-arc personas that span multiple sessions).  It has no operator
  verb: the one RFC 0031 planned, ``persatrix memory recall
  --all-sessions``, is for ``"*"`` and is unbuilt (ISSUE-0086).

Complements the unit-level §D tier pins:

* :mod:`tests.unit.python.test_episodic_session_scope` — episodic +
  notes tier predicate shape.
* :mod:`tests.unit.python.test_relationship_session_scope` —
  relationship tier (including PR 5 / ISSUE-0080 interactions fix).
* :mod:`tests.unit.python.test_facts_session_scope` — facts tier
  (including PR 5 / ISSUE-0079 supersede fix).
* :mod:`tests.unit.python.test_session_recall_default_path` — a source
  scan of the prompt-path modules that must never pick ``"*"``
  themselves, and the facade forwarding pins.

What this file adds is the **end-to-end** proof: every tier, every
mode, single ``MemoryStore`` facade — the operator-visible contract
the Phase 2 RFC promises.  A future plan author cannot regress F-3
without tripping at least one of these tests.

The persona prompt path reads wider than a tier's default: since RFC 0049
PR 4 a ``cross_room: live`` turn widens facts and episodes behind the §D
gate — pinned by :mod:`tests.integration.test_prompt_path_sessions`.
"""

from __future__ import annotations

# ``facade_factory`` (one session's tiers on a shared database) comes from
# ``_session_tiers_helpers.py`` through ``conftest.py``.

# ─── Single-session arc: continuity within the session ──────


class TestSingleSessionArcContinuity:
    """Within one session, a multi-event arc reads back end-to-end.

    The dementia-test bridge: every persona-memory tier (episodic,
    relationship interactions, facts, notes) preserves the within-arc
    continuity that MT-MEMORY-005 demands, under the §D default
    ``sessions=None`` recall.  If any tier silently narrows below the
    active session, this test catches it.
    """

    async def test_arc_reads_back_through_every_tier(
        self, facade_factory,
    ) -> None:
        # One operator session id spans the whole arc.
        b = await facade_factory("arc-1")

        # Episodic write — a fingerprint event.
        await b.facade.store_observation("met alice at the lake")
        # Notes — agent-authored knowledge.
        await b.notes.store_note(
            "alice", "alice is the lake guide", session_id="arc-1",
        )
        # Relationship interaction — registers the peer.
        await b.rels.record_interaction(
            "alice", "task_delegation", outcome="kayak-rental",
            session_id="arc-1",
        )
        # Fact — declarative subject/predicate.
        await b.facts.store(
            subject="alice", predicate="works_at", object="lakeshore",
            source_interaction_id="ix-arc-1", asserted_at=1000.0,
            session_id="arc-1",
        )

        # Default recall through the facade — same session id, every
        # tier returns the arc's content.
        episodes = await b.facade.retrieve_relevant("alice lake")
        assert any("alice" in e.content for e in episodes), (
            "single-session arc: episodic tier dropped the arc's row"
        )

        notes = await b.notes.recall_notes("alice")
        assert any("guide" in n.content for n in notes), (
            "single-session arc: notes tier dropped the arc's row"
        )

        summary = await b.rels.get_relationship_summary("alice")
        assert summary.interaction_count == 1, (
            f"single-session arc: interaction_count = {summary.interaction_count}"
        )
        assert any(
            i.outcome == "kayak-rental" for i in summary.recent_interactions
        )

        facts = await b.facts.recall(subject="alice")
        assert any(f.object == "lakeshore" for f in facts), (
            "single-session arc: facts tier dropped the arc's row"
        )


# ─── Multi-session: no bleed ────────────────────────────────


class TestMultiSessionDefaultIsolation:
    """A second arc under a new session id does NOT surface the first
    arc's content via the §D default — the F-3 closer, end to end.
    """

    async def test_arc_2_default_recall_excludes_arc_1(
        self, facade_factory,
    ) -> None:
        # Arc 1 writes — full quartet of tiers.
        b1 = await facade_factory("arc-1")
        await b1.facade.store_observation("met alice at the lake")
        await b1.notes.store_note(
            "alice", "alice is the lake guide", session_id="arc-1",
        )
        await b1.rels.record_interaction(
            "alice", "task_delegation", outcome="arc1-outcome",
            session_id="arc-1",
        )
        await b1.facts.store(
            subject="alice", predicate="works_at", object="lakeshore",
            source_interaction_id="ix-1", asserted_at=1000.0,
            session_id="arc-1",
        )

        # Arc 2 — same agent, same DB, fresh session id.  Nothing from
        # arc 1 must surface via the §D default.
        b2 = await facade_factory("arc-2")
        episodes = await b2.facade.retrieve_relevant("alice lake")
        assert not any("alice" in e.content for e in episodes), (
            "F-3 read-side leak: arc-1 episodic row surfaced in arc-2"
        )

        notes = await b2.notes.recall_notes("alice")
        assert notes == [], (
            "F-3 read-side leak: arc-1 notes row surfaced in arc-2"
        )

        # The relationship row is tagged arc-1; default summary in
        # arc-2 collapses to the no-relationship branch.
        summary = await b2.rels.get_relationship_summary("alice")
        assert summary.interaction_count == 0, (
            f"F-3 read-side leak: arc-1 interaction surfaced via summary "
            f"({summary.interaction_count})"
        )
        assert summary.recent_interactions == []

        # Facts: arc-1 row is tagged arc-1; arc-2's default filter
        # collapses to active + legacy → no row.
        facts = await b2.facts.recall(subject="alice")
        assert facts == [], (
            "F-3 read-side leak: arc-1 fact surfaced in arc-2"
        )


# ─── Multi-session: write-side isolation (ISSUE-0079/0080 closers) ──


class TestMultiSessionWriteSideIsolation:
    """The PR 5 write-side fixes: cross-session writes cannot
    contaminate another session's view by side effect.

    Pins the integration-level proof that ISSUE-0079 (facts supersede)
    and ISSUE-0080 (interactions leak) are closed at the facade
    boundary — the unit pins assert it at the tier layer.
    """

    async def test_arc_2_fact_does_not_supersede_arc_1_fact(
        self, facade_factory,
    ) -> None:
        """ISSUE-0079 closer: a later ``(subject, predicate)`` write in
        arc 2 does not retroactively erase arc 1's row from arc 1's view.
        """
        b1 = await facade_factory("arc-1")
        await b1.facts.store(
            subject="alice", predicate="lives_in", object="A",
            source_interaction_id="ix-1", asserted_at=1000.0,
            session_id="arc-1",
        )

        # Arc 2 writes the same predicate with a later asserted_at.
        # Pre-fix: this supersedes the arc-1 row globally.
        b2 = await facade_factory("arc-2")
        await b2.facts.store(
            subject="alice", predicate="lives_in", object="B",
            source_interaction_id="ix-2", asserted_at=2000.0,
            session_id="arc-2",
        )

        # Back to arc 1: the arc-1 row is still live.
        b1b = await facade_factory("arc-1")
        facts = await b1b.facts.recall(subject="alice")
        objects = {f.object for f in facts}
        assert objects == {"A"}, (
            f"ISSUE-0079: arc-2 write contaminated arc-1's view "
            f"(saw objects={objects})"
        )

    async def test_summary_count_does_not_inflate_across_sessions(
        self, facade_factory,
    ) -> None:
        """ISSUE-0080 closer: ``interaction_count`` returned by
        ``get_relationship_summary`` reflects only the active session's
        rows, even when the relationship row is visible.
        """
        # First-seen under arc-1 — relationships row tagged arc-1.
        b1 = await facade_factory("arc-1")
        await b1.rels.record_interaction(
            "alice", "task_delegation", outcome="arc1-1",
            session_id="arc-1",
        )
        await b1.rels.record_interaction(
            "alice", "task_delegation", outcome="arc1-2",
            session_id="arc-1",
        )
        # A cross-session interaction (same agent, same DB, tagged arc-2).
        # The ON-CONFLICT branch on relationships bumps the column count
        # to 3 on the original arc-1 row — but the summary surface must
        # derive its count from the filtered interactions subquery and
        # return 2 (the arc-1-tagged rows only).
        await b1.rels.record_interaction(
            "alice", "task_delegation", outcome="arc2-1",
            session_id="arc-2",
        )

        summary = await b1.rels.get_relationship_summary("alice")
        assert summary.interaction_count == 2, (
            f"ISSUE-0080: count inflated by cross-session row "
            f"({summary.interaction_count})"
        )
        outcomes = {i.outcome for i in summary.recent_interactions}
        assert outcomes == {"arc1-1", "arc1-2"}, (
            f"ISSUE-0080: cross-session outcome leaked into recent_interactions "
            f"({outcomes})"
        )


# ─── Cross-session continuity by opt-in ────────────────────


class TestCrossSessionContinuityByOptIn:
    """Explicit ``sessions=[arc1, arc2]`` reads across both arcs — the
    operator-facing bridge for long-arc personas that legitimately span
    multiple sessions.
    """

    async def test_explicit_list_unions_both_arcs(
        self, facade_factory,
    ) -> None:
        b1 = await facade_factory("arc-1")
        await b1.facade.store_observation("met alice at the lake")
        b2 = await facade_factory("arc-2")
        await b2.facade.store_observation("kayaked with alice")

        # Default in arc-2 sees only arc-2 + legacy (no arc-1 row).
        default_eps = await b2.facade.retrieve_relevant("alice")
        default_contents = {e.content for e in default_eps}
        assert any("kayaked" in c for c in default_contents)
        assert not any("met alice" in c for c in default_contents)

        # Explicit cross-session list sees both arcs.
        cross_eps = await b2.facade.retrieve_relevant(
            "alice", sessions=["arc-1", "arc-2"],
        )
        cross_contents = {e.content for e in cross_eps}
        assert any("kayaked" in c for c in cross_contents)
        assert any("met alice" in c for c in cross_contents), (
            "explicit sessions=[arc1, arc2] must surface arc-1's row in arc-2"
        )
