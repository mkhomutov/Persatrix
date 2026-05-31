"""ISSUE-0085 PR 6 — the F-3 *structural* run-isolation acceptance gate.

Phase 2 (`tests/integration/test_session_continuity.py`) closed the
**recall** half of F-3: a second run under a new ``PERSATRIX_SESSION_ID``
does not surface the prior run's rows.  But session is now
**room-continuity** (`docs/memory-scope-axes.md`) — its recall predicate
unions a ``legacy`` carve-out *for* continuity, and the
relationship / person-fact tiers are keyed on the *participant*, so a
rerun that merely renames the channel still inherits old trust and
person-facts.  That residue is the **structural** half of F-3, and it
lives on the orthogonal :mod:`agents.epoch_id` axis.

This file is the acceptance gate named in
:doc:`the epoch PR plan </rfcs/0031-epoch-pr-plan>` PR 6 and the
:doc:`v0.3.5 plan </v0.3.5-plan>` Phase 3b: drive a *real rerun* under a
fresh epoch and assert it inherits **nothing** — across every
persona-memory tier at once (episodes, relationship trust + interaction
history, person-facts).  The unit pins prove the predicate shape per
tier (`tests/unit/python/test_epoch_filter.py`,
`test_epoch_scope.py`); what this adds is the end-to-end property a
future refactor cannot regress without tripping.

The decisive design choice vs. :file:`test_session_continuity.py`:
**both runs hold the same ``PERSATRIX_SESSION_ID`` and the same
``--user`` (``alice``).**  Only :envvar:`PERSATRIX_EPOCH` changes.  That
is what makes this the *structural* gate rather than a second recall
test — it proves epoch isolates the participant-keyed residue a fresh
channel name leaves behind.  And because epoch is **strict equality with
no carve-out** (contrast the session ``legacy`` union), a row written
under the default ``live`` epoch is *also* invisible to a fresh epoch —
the property :class:`TestEpochHasNoCarveOut` pins directly against the
session axis's behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agents.epoch_id import DEFAULT_EPOCH_ID
from agents.memory.facade import MemoryStore
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory

#: Held constant across every run in this file.  Pinning the session
#: (room) and the user while varying only the epoch is what isolates the
#: epoch axis as the *cause* of the reset — a fresh channel name alone
#: cannot reach the participant-keyed relationship / person-fact tiers.
SHARED_ROOM = "shared-room"
ALICE = "alice"


class _Bundle:
    """Construction-time snapshot of the participant-keyed tiers for one
    epoch.  Mirrors how :class:`agents.base.BaseAgent` /
    persona-runtime ``initialize_memory`` wire the tiers under a single
    resolved :envvar:`PERSATRIX_EPOCH` — the env value is captured at
    construction (``resolve_epoch_id_silent`` into ``_active_epoch_id``),
    so an already-built bundle keeps its epoch even after a later build
    overrides the env var.  That snapshot is the property under test
    (pinned by ``test_prior_run_bundle_retains_its_epoch_snapshot``): a
    second run under a fresh epoch tags and filters on its own id.
    """

    def __init__(self, facade: MemoryStore, rels: RelationshipMemory,
                 facts: FactStore) -> None:
        self.facade = facade
        self.rels = rels
        self.facts = facts


@pytest.fixture
async def epoch_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator:
    """Build a :class:`_Bundle` keyed to a named ``PERSATRIX_EPOCH``.

    Every tier across every epoch shares one ``db_path`` — the cross-run
    state-bleed surface this gate exists to close.  ``PERSATRIX_SESSION_ID``
    is held at :data:`SHARED_ROOM` for *all* builds so the session axis is
    identical between runs and cannot be what isolates them; only the
    epoch differs.
    """
    db_path = tmp_path / "shared.db"
    bundles: list[_Bundle] = []
    monkeypatch.setenv("PERSATRIX_SESSION_ID", SHARED_ROOM)

    async def _build(epoch_id: str, agent_id: str = "ember-owl") -> _Bundle:
        monkeypatch.setenv("PERSATRIX_EPOCH", epoch_id)
        fac = MemoryStore(agent_id=agent_id, db_path=str(db_path))
        await fac.initialize()
        rels = RelationshipMemory(agent_id=agent_id, db_path=str(db_path))
        await rels.initialize()
        facts = FactStore(agent_id=agent_id, db_path=str(db_path))
        await facts.initialize()
        bundles.append(_Bundle(fac, rels, facts))
        return bundles[-1]

    yield _build
    for b in bundles:
        await b.facade.close()
        await b.rels.close()
        await b.facts.close()


async def _seed_run(b: _Bundle) -> None:
    """Write the full participant-keyed quartet under ``b``'s epoch.

    Episodes (room-scoped) + relationship trust + relationship
    interaction history + a person-fact (all participant-scoped) — the
    surfaces a rerun must not inherit.  Session id is :data:`SHARED_ROOM`
    on every write so isolation cannot be attributed to the session axis.
    """
    await b.facade.store_observation("kayaked with alice on the lake")
    await b.rels.update_trust(ALICE, 0.2, "delivered the kayak on time")
    await b.rels.record_interaction(
        ALICE, "task_delegation", outcome="kayak-rental",
        session_id=SHARED_ROOM,
    )
    await b.facts.store(
        subject=ALICE, predicate="works_at", object="lakeshore",
        source_interaction_id="ix-run-1", asserted_at=1000.0,
        session_id=SHARED_ROOM,
    )


# ─── The F-3 structural-isolation gate ──────────────────────


class TestEpochRunIsolation:
    """A rerun under a fresh epoch inherits none of the prior run's
    state — the structural half of F-3, end to end.
    """

    async def test_rerun_under_fresh_epoch_inherits_nothing(
        self, epoch_factory,
    ) -> None:
        # Run 1 under epoch ``run-1`` — establish trust, an interaction,
        # a person-fact, and an episode about alice.
        run1 = await epoch_factory("run-1")
        await _seed_run(run1)

        # Sanity: run 1 reads its own state back across every tier this
        # test later asserts is isolated (episode, trust + interactions,
        # person-fact) — so each isolation assertion below is non-vacuous
        # in its own right, not only via TestWithinEpochContinuity.  The
        # filter narrows by epoch; it is not hiding everything.
        summary1 = await run1.rels.get_relationship_summary(ALICE)
        assert summary1.trust_score > 0.5, (
            "within-epoch sanity: run-1 must see the trust it built "
            f"(got {summary1.trust_score})"
        )
        assert summary1.interaction_count == 1
        episodes1 = await run1.facade.retrieve_relevant("alice")
        assert any("alice" in e.content for e in episodes1), (
            "within-epoch sanity: run-1 must retrieve the episode it stored"
        )
        facts1 = await run1.facts.recall(subject=ALICE)
        assert any(f.object == "lakeshore" for f in facts1), (
            "within-epoch sanity: run-1 must recall the person-fact it stored"
        )

        # Run 2 — SAME room, SAME user, fresh epoch ``run-2``.  This is
        # the rerun a fresh channel name alone cannot isolate: the
        # relationship / person-fact rows are keyed on alice, not the
        # room.  Epoch is what resets them.
        run2 = await epoch_factory("run-2")

        # Episodes — room-scoped, but tagged run-1's epoch.
        episodes = await run2.facade.retrieve_relevant("alice")
        assert not any("alice" in e.content for e in episodes), (
            "F-3 structural leak: run-1 episode surfaced under a fresh epoch"
        )

        # Relationship trust + history — the participant-keyed residue.
        summary2 = await run2.rels.get_relationship_summary(ALICE)
        assert summary2.trust_score == 0.5, (
            "F-3 structural leak: run-1 trust inherited under a fresh epoch "
            f"(got {summary2.trust_score}, expected the 0.5 no-relationship "
            "default)"
        )
        assert summary2.interaction_count == 0, (
            "F-3 structural leak: run-1 interaction surfaced under a fresh "
            f"epoch ({summary2.interaction_count})"
        )
        assert summary2.recent_interactions == []

        # Person-facts — survive a room rename; only epoch resets them.
        facts = await run2.facts.recall(subject=ALICE)
        assert facts == [], (
            "F-3 structural leak: run-1 person-fact surfaced under a fresh "
            f"epoch ({facts!r})"
        )

    async def test_prior_run_bundle_retains_its_epoch_snapshot(
        self, epoch_factory,
    ) -> None:
        """An already-built bundle keeps reading its own epoch after a
        later build overrides ``PERSATRIX_EPOCH``.

        The complement to
        :meth:`test_rerun_under_fresh_epoch_inherits_nothing`: that test
        proves the *new* run sees nothing; this proves the **snapshot**
        is what isolates them.  Each tier resolves its ``epoch_id`` from
        the construction-time snapshot (``resolve_epoch_id_silent`` into
        ``_active_epoch_id``), never the live env var
        (``agents.memory._epoch_filter.resolve_active_epoch`` is
        ``current_epoch_id() or snapshot`` — no env read) — so building a
        fresh ``run-2`` bundle, which flips the env to ``run-2``, must not
        retroactively blind ``run-1``'s bundle to its own rows.  Were a
        tier to read the env at query time instead, run-1 would resolve
        to ``run-2`` here and lose its data: the regression this pins.
        """
        run1 = await epoch_factory("run-1")
        await _seed_run(run1)

        # A later build flips PERSATRIX_EPOCH to ``run-2`` — the env the
        # run-1 snapshot must ignore.  (Its bundle is unused here.)
        await epoch_factory("run-2")

        # run-1 still reads its own arc back across every tier: the env
        # moved on, the construction-time snapshot did not.
        summary = await run1.rels.get_relationship_summary(ALICE)
        assert summary.trust_score > 0.5, (
            "snapshot retention: run-1 lost its trust once run-2 was built "
            f"(got {summary.trust_score}) — a tier read the env at query time"
        )
        assert summary.interaction_count == 1
        facts = await run1.facts.recall(subject=ALICE)
        assert any(f.object == "lakeshore" for f in facts), (
            "snapshot retention: run-1 lost its fact once run-2 was built"
        )
        episodes = await run1.facade.retrieve_relevant("alice")
        assert any("alice" in e.content for e in episodes), (
            "snapshot retention: run-1 lost its episode once run-2 was built"
        )


# ─── Strict equality: no carve-out (the contrast with session) ──


class TestEpochHasNoCarveOut:
    """The default ``live`` epoch is NOT a cross-epoch carve-out.

    Where the session axis unions ``legacy`` so pre-RFC rows stay visible
    from every session (`test_session_recall_isolation.py
    ::test_legacy_rows_visible_from_every_session`), epoch is strict
    equality: a row written under the default ``live`` epoch is invisible
    to any other epoch.  This is the inverted sibling of the session
    legacy-carve-out test, and the property that makes ``epoch`` an
    isolation axis rather than a continuity one.
    """

    async def test_live_epoch_rows_invisible_to_a_fresh_epoch(
        self, epoch_factory,
    ) -> None:
        # Seed under the default epoch — the value production never
        # changes, and the one a session-style carve-out would make
        # universally visible.
        live = await epoch_factory(DEFAULT_EPOCH_ID)
        await _seed_run(live)

        run2 = await epoch_factory("run-2")

        episodes = await run2.facade.retrieve_relevant("alice")
        assert not any("alice" in e.content for e in episodes), (
            "epoch carve-out leak: a 'live'-epoch episode was visible to "
            "'run-2' — epoch must have no legacy-style carve-out"
        )
        summary = await run2.rels.get_relationship_summary(ALICE)
        assert summary.trust_score == 0.5
        assert summary.interaction_count == 0
        facts = await run2.facts.recall(subject=ALICE)
        assert facts == [], (
            "epoch carve-out leak: a 'live'-epoch person-fact was visible "
            "to 'run-2'"
        )


# ─── Within-epoch continuity (default-epoch behaviour unchanged) ──


class TestWithinEpochContinuity:
    """A single epoch reads its own arc back through every tier.

    The complement to isolation: the strict-equality filter must not
    narrow recall *within* an epoch.  This is the executable proof of the
    plan's "default-epoch (``live``) behaviour is byte-identical to
    pre-migration" acceptance — single-world deployments are unchanged.
    """

    async def test_single_epoch_arc_reads_back(
        self, epoch_factory,
    ) -> None:
        b = await epoch_factory(DEFAULT_EPOCH_ID)
        await _seed_run(b)

        episodes = await b.facade.retrieve_relevant("alice")
        assert any("alice" in e.content for e in episodes), (
            "within-epoch continuity: episodic tier dropped the arc's row"
        )
        summary = await b.rels.get_relationship_summary(ALICE)
        assert summary.trust_score > 0.5, (
            "within-epoch continuity: relationship tier dropped the trust"
        )
        assert summary.interaction_count == 1
        assert any(
            i.outcome == "kayak-rental" for i in summary.recent_interactions
        )
        facts = await b.facts.recall(subject=ALICE)
        assert any(f.object == "lakeshore" for f in facts), (
            "within-epoch continuity: facts tier dropped the arc's row"
        )
