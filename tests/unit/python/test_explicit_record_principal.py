"""ISSUE-0137 — the write boundary is TOLD whose record it is.

The ``(principal, speaker, scope)`` re-key freezes both key halves on the
record at open, but until this change only one of them travelled by the
call.  ``store_episode`` and ``FactStore.store`` took ``speaker_id`` as an
argument and resolved ``principal_id`` from the ambient
``principal_scope`` — so the tenant half of the same key reached the same
row by a different mechanism, and one visible in no signature.

What held it up was a single ``with`` statement (``record_write_scopes``)
at one call site.  Nothing failed if a new derived-write path forgot it:
no exception, no counter, no type error — just a row claiming alice spoke
inside bob's tenant, invisible to alice (``_principal_filter`` is strict
equality with no carve-out) and readable by bob.

These pins hold the inversion the issue asks for: the explicit argument
is the contract, and the ambient scope is defence-in-depth.  The
class that matters is :class:`TestTheCallCarriesIt` — it neutralises the
wrapper and asserts the rows land correctly anyway, which is the whole
of "pinned by a test, not by one ``with`` statement".
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.facts import FactStore
from agents.memory.interactions import Interaction, Turn
from agents.persona_runtime import close_path, fact_extractor
from agents.principal_id import principal_scope

_CLOSER = "p-bob"
_OWNER = "p-alice"


@pytest.fixture
async def facts():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


def _closed_record(principal: str) -> Interaction:
    """A closed record frozen under ``principal`` — what a room fan or an
    ``idle_check`` sweep hands the close path from a DIFFERENT tenant's
    request scope."""
    return Interaction(
        interaction_id=f"i-{principal}",
        scope="group:planning",
        started_at=1_000.0,
        closed_at=1_100.0,
        close_reason=REASON_STRUCTURAL,
        principal_id=principal,
        speaker_id="alice",
        turns=[Turn(at=1_000.0, payload={"sender": "alice"})],
    )


async def _episode_principal(memory: Any, interaction_id: str) -> str:
    db = memory._ensure_db()
    async with db.execute(
        "SELECT principal_id FROM episodes WHERE interaction_id = ?",
        (interaction_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "no episode was written"
    return str(row[0])


async def _fact_principals(store: FactStore) -> list[str]:
    db = store._ensure_db()
    async with db.execute("SELECT principal_id FROM facts") as cursor:
        return [str(r[0]) for r in await cursor.fetchall()]


# ─── Step 1: the two write boundaries take the principal ────


class TestStoreEpisodeTakesThePrincipal:
    async def test_the_argument_wins_over_the_ambient_scope(self, memory):
        """The demonstration in the issue, now the contract: a caller that
        knows whose record this is says so, and is believed."""
        with principal_scope(_CLOSER):
            await memory.store_episode(
                "alice ships the ledger on Friday", {},
                interaction_id="i-alice", speaker_id="alice",
                principal_id=_OWNER,
            )

        assert await _episode_principal(memory, "i-alice") == _OWNER

    async def test_none_still_resolves_ambient(self, memory):
        """``None`` means "resolve ambient", which is what preserves every
        pre-existing caller — the parameter adds a way to be explicit, it
        does not require it."""
        with principal_scope(_CLOSER):
            await memory.store_episode(
                "bob's own turn", {}, interaction_id="i-bob",
            )

        assert await _episode_principal(memory, "i-bob") == _CLOSER


class TestFactStoreTakesThePrincipal:
    async def test_the_argument_wins_over_the_ambient_scope(self, facts):
        with principal_scope(_CLOSER):
            await facts.store(
                subject="alice", predicate="works_at", object="ledger",
                source_interaction_id="i-alice", asserted_at=1_000.0,
                principal_id=_OWNER,
            )

        assert await _fact_principals(facts) == [_OWNER]

    async def test_none_still_resolves_ambient(self, facts):
        with principal_scope(_CLOSER):
            await facts.store(
                subject="bob", predicate="works_at", object="registry",
                source_interaction_id="i-bob", asserted_at=1_000.0,
            )

        assert await _fact_principals(facts) == [_CLOSER]

    async def test_the_supersede_chain_keys_on_the_explicit_principal(
        self, facts,
    ):
        """``principal_id`` is not only the row tag — it is half the key
        the RFC 0026 §F supersession pass matches on.  If the argument
        reached the column but not the chain, one tenant's newer fact
        would retract another's."""
        with principal_scope(_CLOSER):
            await facts.store(
                subject="alice", predicate="works_at", object="ledger",
                source_interaction_id="i-1", asserted_at=1_000.0,
                principal_id=_OWNER,
            )
            await facts.store(
                subject="alice", predicate="works_at", object="registry",
                source_interaction_id="i-2", asserted_at=2_000.0,
                principal_id=_OWNER,
            )

        with principal_scope(_OWNER):
            live = await facts.recall(subject="alice", sessions="*")
        assert [f.object for f in live] == ["registry"]


# ─── Step 4: the pin — the call carries it, not the wrapper ─


@pytest.fixture
def _wrapper_neutralised(monkeypatch):
    """Remove the ambient binding the invariant used to rest on.

    This is the fixture that gives the issue its answer.  Before this
    change, deleting ``record_write_scopes`` changed no signature and
    broke no type — every close-derived row silently acquired the
    closer's tenant.  With the principal travelling by argument, the
    rows must land correctly with the wrapper gone.

    Deliberately scoped to the PRINCIPAL claim: the epoch half still
    rides the wrapper (it has no explicit parameter, and growing one is
    not this issue's scope), so these tests assert tenancy only.
    """
    monkeypatch.setattr(
        close_path, "record_write_scopes",
        lambda interaction: contextlib.nullcontext(),
    )


@pytest.fixture
def _no_phase_two(monkeypatch):
    async def _skip(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(close_path, "finalize_closed_interaction", _skip)


class TestTheCallCarriesIt:
    async def test_episode_lands_under_the_records_principal(
        self, memory, _wrapper_neutralised, _no_phase_two,
    ):
        """The close path hands ``store_episode`` the record's frozen
        tenant beside its frozen speaker, so both halves of the key reach
        the row the same way and a reader of the call site sees both."""
        record = _closed_record(_OWNER)
        with principal_scope(_CLOSER):
            await close_path.persist_closed_interaction(
                episodic=memory, llm_client=MagicMock(), memory_ns=MagicMock(),
                agent_id="test-agent", interaction=record,
                pending_tasks=set(), on_finalized=_noop,
            )

        assert await _episode_principal(memory, record.interaction_id) == _OWNER

    async def test_derived_facts_land_under_the_records_principal(
        self, facts, _wrapper_neutralised,
    ):
        """Phase 2's facts half, driven through the real dispatcher: the
        tuples inherit the source interaction's frozen tenant exactly as
        they already inherit its frozen speaker."""
        record = _closed_record(_OWNER)
        with principal_scope(_CLOSER):
            await fact_extractor.dispatch_facts_from_response(
                fact_store=facts,
                facts_raw=(
                    '[{"subject": "alice", "predicate": "works_at", '
                    '"object": "ledger", "certainty": 0.9}]'
                ),
                interaction=record,
                agent_id="test-agent",
                session_id="legacy",
            )

        assert await _fact_principals(facts) == [_OWNER]

    async def test_the_owner_can_recall_what_the_closer_derived(
        self, memory, _wrapper_neutralised, _no_phase_two,
    ):
        """The consequence, restated without the wrapper: strict-equality
        recall means a mis-tagged row is not merely mislabelled, it is
        lost to its owner and exposed to the closer."""
        record = _closed_record(_OWNER)
        with principal_scope(_CLOSER):
            await close_path.persist_closed_interaction(
                episodic=memory, llm_client=MagicMock(), memory_ns=MagicMock(),
                agent_id="test-agent", interaction=record,
                pending_tasks=set(), on_finalized=_noop,
            )
        await memory.update_episode_summary(
            record.interaction_id, "alice ships the ledger on Friday",
        )

        with principal_scope(_OWNER):
            assert len(await memory.recall(limit=10)) == 1
        with principal_scope(_CLOSER):
            assert await memory.recall(limit=10) == []


async def _noop() -> None:
    return None
