"""Who may read a commitment (RFC 0021 Phase 2 PR 2, the plan's H-3).

A commitment is scoped like a fact: each row carries the session, person
(principal) and epoch of the turn that made it, and every call reads with
the facts tier's filters — strict equality for the person and the epoch,
the room's session plus the ``legacy`` carve-out for the session.  So a
promise made to one signed-in person never reaches another's prompt, and
a fresh epoch starts with none.  The cap on open commitments is the
exception: it counts every person's and every room's, within one epoch.
The same table holds RFC 0012's directive rows; no call here sees one.
Making a commitment and its
lifecycle are in ``test_commitment_store.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest

from agents.clock import FrozenClock
from agents.epoch_id import epoch_scope
from agents.memory.commitments import (
    MAX_OPEN_COMMITMENTS,
    CommitmentCapError,
    CommitmentNotFoundError,
    CommitmentStore,
)
from agents.principal_id import principal_scope
from agents.session_id import session_scope

from ._commitments_migration_helpers import COMMITMENT, DIRECTIVE, insert

_T0 = 2_000_000_000.0
_DUE = _T0 + 3_600.0

Scope = Callable[[str], AbstractContextManager[str]]


@pytest.fixture
async def store():
    s = CommitmentStore(
        agent_id="ember-owl", db_path=":memory:", clock=FrozenClock(at=_T0),
    )
    await s.initialize()
    yield s
    await s.close()


async def _make(store: CommitmentStore, target_party: str | None = "bob") -> str:
    return await store.create(
        description="send Bob the notes", due_at=_DUE, target_party=target_party,
    )


async def _visible(store: CommitmentStore, cid: str) -> dict[str, Any]:
    """What each read returns for ``cid``, and whether each change finds it."""
    seen: dict[str, Any] = {
        "get": await store.get(cid) is not None,
        "list": [c.id for c in await store.list_commitments(state=None)],
        "open": [c.id for c in await store.list_open_commitments()],
        "to": [c.id for c in await store.list_commitments_to("bob", state=None)],
    }
    return seen


_HIDDEN = {"get": False, "list": [], "open": [], "to": []}


def _shown(cid: str) -> dict[str, Any]:
    return {"get": True, "list": [cid], "open": [cid], "to": [cid]}


# ─── Person and epoch: strict equality ──────────────────────


@pytest.mark.parametrize(
    "scope", [principal_scope, epoch_scope], ids=["principal", "epoch"],
)
class TestStrictAxes:
    async def test_another_ones_commitment_is_invisible(
        self, store: CommitmentStore, scope: Scope,
    ):
        with scope("alice"):
            cid = await _make(store)
        with scope("bob"):
            assert await _visible(store, cid) == _HIDDEN
        with scope("alice"):
            assert await _visible(store, cid) == _shown(cid)

    async def test_the_unscoped_default_sees_none_of_them(
        self, store: CommitmentStore, scope: Scope,
    ):
        """The default person and epoch are values, not carve-outs."""
        with scope("alice"):
            cid = await _make(store)
        assert await _visible(store, cid) == _HIDDEN

    @pytest.mark.parametrize("call", ["fulfill", "cancel"])
    async def test_another_ones_commitment_cannot_be_closed(
        self, store: CommitmentStore, scope: Scope, call: str,
    ):
        with scope("alice"):
            cid = await _make(store)
        with scope("bob"), pytest.raises(CommitmentNotFoundError):
            await getattr(store, call)(cid)
        with scope("alice"):
            commitment = await store.get(cid)
        assert commitment is not None
        assert commitment.state == "open"


# ─── The cap: one count per persona, in each epoch ──────────


async def test_the_cap_counts_every_persons_commitments(store: CommitmentStore):
    """H-7 bounds the persona's open commitments, not each person's, so
    one person's 500 refuse another's next one (the maintainer's ruling
    on PR 2's F-1)."""
    db = store._ensure_db()
    for n in range(MAX_OPEN_COMMITMENTS):
        await insert(db, {**COMMITMENT, "id": f"c-{n}", "principal_id": "alice"})
    with principal_scope("bob"), pytest.raises(CommitmentCapError):
        await _make(store)


async def test_the_cap_counts_only_the_epochs_commitments(store: CommitmentStore):
    """An epoch is an isolated run: another epoch's rows cannot be read or
    closed from this one, so they do not count toward its cap."""
    db = store._ensure_db()
    for n in range(MAX_OPEN_COMMITMENTS):
        await insert(db, {**COMMITMENT, "id": f"c-{n}", "epoch_id": "run-1"})
    with epoch_scope("run-1"), pytest.raises(CommitmentCapError):
        await _make(store)
    with epoch_scope("run-2"):
        await _make(store)


# ─── Session: the room plus the legacy carve-out ────────────


class TestSession:
    async def test_another_rooms_commitment_is_invisible(self, store: CommitmentStore):
        with session_scope("room-a"):
            cid = await _make(store)
        with session_scope("room-b"):
            assert await _visible(store, cid) == _HIDDEN
            with pytest.raises(CommitmentNotFoundError):
                await store.fulfill(cid)
        with session_scope("room-a"):
            assert await _visible(store, cid) == _shown(cid)

    async def test_a_legacy_commitment_is_visible_from_every_room(
        self, store: CommitmentStore,
    ):
        cid = await _make(store)
        for room in ("room-a", "room-b"):
            with session_scope(room):
                assert await _visible(store, cid) == _shown(cid)
        with session_scope("room-b"):
            await store.fulfill(cid)

    async def test_the_cap_spans_rooms(self, store: CommitmentStore):
        """The cap bounds the persona's open commitments, not a room's, so
        it cannot pass it by spreading them over rooms."""
        db = store._ensure_db()
        for n in range(MAX_OPEN_COMMITMENTS):
            room = "room-a" if n % 2 else "room-b"
            await insert(db, {**COMMITMENT, "id": f"c-{n}", "session_id": room})
        with session_scope("room-c"), pytest.raises(CommitmentCapError):
            await _make(store)


# ─── What a row is stamped with ─────────────────────────────


async def test_a_row_carries_the_scope_of_the_turn_that_made_it(store: CommitmentStore):
    with session_scope("room-a"), principal_scope("alice"), epoch_scope("run-7"):
        cid = await _make(store)
        commitment = await store.get(cid)
    assert commitment is not None
    assert commitment.session_id == "room-a"
    async with store._ensure_db().execute(
        "SELECT agent_id, kind, session_id, principal_id, epoch_id "
        "FROM commitments WHERE id = ?",
        (cid,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("ember-owl", "commitment", "room-a", "alice", "run-7")


# ─── Rows that are not this persona's commitments ───────────


class TestOtherRows:
    async def test_a_directive_is_invisible_to_every_call(self, store: CommitmentStore):
        """The table's other kind, RFC 0012's, under the same scope, with a
        target and a due time so every read could match it."""
        directive = {**DIRECTIVE, "target_party": "bob", "due_at": _DUE}
        await insert(store._ensure_db(), directive)
        assert await _visible(store, "d-1") == _HIDDEN
        for call in ("fulfill", "cancel"):
            with pytest.raises(CommitmentNotFoundError):
                await getattr(store, call)("d-1")

    async def test_directives_do_not_count_toward_the_cap(self, store: CommitmentStore):
        """Held twice over: the cap counts ``kind = 'commitment'``, and the
        table gives a directive no ``state`` to be ``open``.  This pins the
        outcome against a later change to either, not which one holds it."""
        db = store._ensure_db()
        for n in range(MAX_OPEN_COMMITMENTS):
            await insert(db, {**DIRECTIVE, "id": f"d-{n}"})
        await _make(store)

    async def test_another_personas_commitment_is_invisible(self, store: CommitmentStore):
        db = store._ensure_db()
        await insert(db, {**COMMITMENT, "agent_id": "quiet-heron"})
        assert await _visible(store, "c-1") == _HIDDEN
        for call in ("fulfill", "cancel"):
            with pytest.raises(CommitmentNotFoundError):
                await getattr(store, call)("c-1")
        async with db.execute("SELECT state FROM commitments WHERE id = 'c-1'") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "open"

    async def test_another_personas_commitments_do_not_count_toward_the_cap(
        self, store: CommitmentStore,
    ):
        """Personas may share one memory file (``data/memory.db``)."""
        db = store._ensure_db()
        for n in range(MAX_OPEN_COMMITMENTS):
            await insert(db, {**COMMITMENT, "id": f"c-{n}", "agent_id": "quiet-heron"})
        await _make(store)
