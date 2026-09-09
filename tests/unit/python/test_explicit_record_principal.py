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
is the contract, and the ambient scope is defence-in-depth.  The class
that matters is :class:`TestTheCallCarriesIt` — it neutralises the
wrapper and asserts that the two tiers which TAKE the argument land
their rows correctly anyway, which is the whole of "pinned by a test,
not by one ``with`` statement".  Both halves are driven through
``persist_closed_interaction``, because a test that calls the facts
dispatcher directly never enters the wrapper and so could not tell the
argument from the scope.

The boundary's OWN contract — that an explicit value is normalised the
way the ambient one always was — is pinned in
:class:`TestTheBoundaryNormalisesWhatItIsTold`.  The tenancy predicate is
strict equality, so a principal that reaches the column unstripped is not
mislabelled but orphaned: no caller can ever name it.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.memory.facts import FactStore
from agents.persona_runtime import finalize_close
from agents.principal_id import DEFAULT_PRINCIPAL_ID, principal_scope

from ._close_path_test_helpers import (
    CLOSER,
    OWNER,
    closed_record,
    episode_principal,
    persist,
)


@pytest.fixture
async def facts():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _fact_principals(store: FactStore) -> list[str]:
    db = store._ensure_db()
    async with db.execute("SELECT principal_id FROM facts") as cursor:
        return [str(r[0]) for r in await cursor.fetchall()]


class _FactsOnlyNamespace:
    """The one attribute Phase 2's facts branch reads off ``memory_ns``."""

    def __init__(self, store: FactStore) -> None:
        self.facts = store


# ─── Step 1: the two write boundaries take the principal ────


class TestStoreEpisodeTakesThePrincipal:
    async def test_the_argument_wins_over_the_ambient_scope(self, memory):
        """The demonstration in the issue, now the contract: a caller that
        knows whose record this is says so, and is believed."""
        with principal_scope(CLOSER):
            await memory.store_episode(
                "alice ships the ledger on Friday", {},
                interaction_id="i-alice", speaker_id="alice",
                principal_id=OWNER,
            )

        assert await episode_principal(memory, "i-alice") == OWNER


class TestFactStoreTakesThePrincipal:
    async def test_the_argument_wins_over_the_ambient_scope(self, facts):
        with principal_scope(CLOSER):
            await facts.store(
                subject="alice", predicate="works_at", object="ledger",
                source_interaction_id="i-alice", asserted_at=1_000.0,
                principal_id=OWNER,
            )

        assert await _fact_principals(facts) == [OWNER]

    async def test_the_supersede_chain_keys_on_the_explicit_principal(
        self, facts,
    ):
        """``principal_id`` is not only the row tag — it is half the key
        the RFC 0026 §F supersession pass matches on.  If the argument
        reached the column but not the chain, one tenant's newer fact
        would retract another's."""
        with principal_scope(CLOSER):
            await facts.store(
                subject="alice", predicate="works_at", object="ledger",
                source_interaction_id="i-1", asserted_at=1_000.0,
                principal_id=OWNER,
            )
            await facts.store(
                subject="alice", predicate="works_at", object="registry",
                source_interaction_id="i-2", asserted_at=2_000.0,
                principal_id=OWNER,
            )

        with principal_scope(OWNER):
            live = await facts.recall(subject="alice", sessions="*")
        assert [f.object for f in live] == ["registry"]


# ─── The boundary normalises what it is told ────────────────


class TestTheBoundaryNormalisesWhatItIsTold:
    """A new public write parameter is a new way to strand a row.

    Every pre-existing route to this column was normalised — the
    ContextVar is set by ``principal_scope``, which runs
    ``normalize_principal_id``, and the construction snapshot comes from
    ``resolve_principal_id_silent``.  The explicit argument has to run
    the same normaliser, or a padded value lands a row that strict
    equality can never match from either side, and a blank one means
    something different here than it means everywhere else on the axis.
    """

    async def test_a_padded_principal_is_stripped_before_it_is_stored(
        self, memory,
    ):
        with principal_scope(CLOSER):
            await memory.store_episode(
                "alice ships the ledger", {},
                interaction_id="i-padded", principal_id=f"  {OWNER}  ",
            )

        assert await episode_principal(memory, "i-padded") == OWNER
        with principal_scope(OWNER):
            assert len(await memory.recall(limit=10)) == 1

    async def test_a_padded_principal_is_stripped_on_the_facts_side(
        self, facts,
    ):
        with principal_scope(CLOSER):
            await facts.store(
                subject="alice", predicate="works_at", object="ledger",
                source_interaction_id="i-1", asserted_at=1_000.0,
                principal_id=f"  {OWNER}  ",
            )

        assert await _fact_principals(facts) == [OWNER]
        with principal_scope(OWNER):
            assert len(await facts.recall(subject="alice", sessions="*")) == 1

    async def test_a_blank_principal_is_the_default_not_the_closers(
        self, memory,
    ):
        """``""`` is the unset sentinel the sibling axes on this same
        record use, and ``principal_scope("")`` resolves it to the
        default tenant.  The argument must not read it as "no opinion,
        use whoever is running" — that is the ISSUE-0137 bug itself."""
        with principal_scope(CLOSER):
            await memory.store_episode(
                "a record minted with no tenant", {},
                interaction_id="i-blank", principal_id="",
            )

        assert await episode_principal(
            memory, "i-blank",
        ) == DEFAULT_PRINCIPAL_ID

    async def test_a_blank_principal_is_the_default_on_the_facts_side(
        self, facts,
    ):
        with principal_scope(CLOSER):
            await facts.store(
                subject="alice", predicate="works_at", object="ledger",
                source_interaction_id="i-1", asserted_at=1_000.0,
                principal_id="",
            )

        assert await _fact_principals(facts) == [DEFAULT_PRINCIPAL_ID]

    async def test_none_still_resolves_ambient(self, memory):
        """``None`` — and ONLY ``None`` — means "resolve ambient", which
        is what preserves every pre-existing caller."""
        with principal_scope(CLOSER):
            await memory.store_episode(
                "bob's own turn", {}, interaction_id="i-bob",
            )

        assert await episode_principal(memory, "i-bob") == CLOSER


# ─── Step 4: the pin — the call carries it, not the wrapper ─


class TestTheCallCarriesIt:
    async def test_episode_lands_under_the_records_principal(
        self, memory, wrapper_neutralised, no_phase_two,
    ):
        """The close path hands ``store_episode`` the record's frozen
        tenant beside its frozen speaker, so both halves of the key reach
        the row the same way and a reader of the call site sees both."""
        record = closed_record(OWNER)
        with principal_scope(CLOSER):
            await persist(memory, record)

        assert await episode_principal(memory, record.interaction_id) == OWNER

    async def test_derived_facts_land_under_the_records_principal(
        self, memory, facts, wrapper_neutralised, monkeypatch,
    ):
        """Phase 2's facts half, driven through the REAL close path.

        The wrapper is gone and the Phase-2 task is awaited, so the only
        thing left carrying the tenant across the ``asyncio.create_task``
        boundary is the argument ``dispatch_facts_from_response`` threads
        from the record's frozen key.  Calling the dispatcher directly
        would prove only that a keyword is forwarded — it never enters
        the wrapper, so it could not tell the two mechanisms apart.
        """
        async def _summarise(
            _client: Any, _agent_id: str, _interaction: Any,
        ) -> tuple[str, bool, str, dict[str, str]]:
            return (
                "alice ships the ledger on Friday",
                False,
                '[{"subject": "alice", "predicate": "works_at", '
                '"object": "ledger", "certainty": 0.9}]',
                {},
            )

        monkeypatch.setattr(
            finalize_close, "summarize_closed_interaction", _summarise,
        )
        # The relationship bump is out of scope here: it has no tenant
        # argument, so with the wrapper gone it would resolve ambient by
        # design (see the fixture's docstring).
        monkeypatch.setattr(
            finalize_close, "record_closed_interaction",
            lambda *a, **k: _noop_coro(),
        )

        record = closed_record(OWNER)
        tasks: set[Any] = set()
        with principal_scope(CLOSER):
            await persist(
                memory, record,
                memory_ns=_FactsOnlyNamespace(facts),
                pending_tasks=tasks,
            )
            for task in list(tasks):
                await task

        assert await _fact_principals(facts) == [OWNER]


async def _noop_coro() -> None:
    return None
