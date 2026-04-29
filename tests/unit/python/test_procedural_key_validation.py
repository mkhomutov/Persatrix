"""PR 5 R2 M1 procedural-key validation pins (RFC 0008 PR 6b).

The :meth:`MemoryFacade.store_procedure` boundary rejects keys whose
characters fall outside ``^[A-Za-z0-9._-]+$`` so a future SQL or
log-pipeline change cannot be blindsided by an exotic Unicode key.

(Note: :meth:`MemoryFacade.retrieve_procedures` takes a free-text
``query``, not a key — that path is escaped for ``LIKE`` semantics by
:func:`agents.memory.episodic_procedural._escape_like` and is not
covered by the regex validator.  PR 6b deep review Should-Fix #2.)

The escape behaviour at the helper level
(:func:`agents.memory.episodic_procedural.refresh_confidence` /
:func:`agents.memory.episodic_procedural.recall_procedures`) is still
pinned by ``test_memory_decay_review_pins.py`` as defence-in-depth so
that callers bypassing the facade (or a future relaxation of the
regex) still see correct LIKE-escape semantics.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from agents.memory.facade import MemoryFacade


@pytest.fixture
async def facade() -> AsyncGenerator[MemoryFacade, None]:
    fac = MemoryFacade(agent_id="proc-key-test", db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


@pytest.mark.parametrize(
    "bad_key",
    [
        "50% off",          # space + percent
        "with/slash",       # slash
        "naïve",            # non-ASCII letter
        "with\nnewline",    # control char
        "",                 # empty
        "x" * 257,          # over the 256-char cap
    ],
)
async def test_store_procedure_rejects_invalid_keys(
    facade: MemoryFacade, bad_key: str,
) -> None:
    """Every invalid-shape key must surface as ``ValueError`` at the
    facade boundary (PR 5 R2 M1 — breaking change vs v0.2.x)."""
    with pytest.raises(ValueError):
        await facade.store_procedure(bad_key, "body", confidence=0.9)


@pytest.mark.parametrize(
    "good_key",
    [
        "tools.deploy",
        "deploy_v2",
        "ABC-123",
        "x",                # single char
        "a" * 256,          # exactly at the 256-char cap
    ],
)
async def test_store_procedure_accepts_canonical_keys(
    facade: MemoryFacade, good_key: str,
) -> None:
    """The accept-set covers the canonical alphabet plus boundary lengths."""
    await facade.store_procedure(good_key, "body", confidence=0.9)
    # Round-trip the row to confirm the insert path completed.
    entries = await facade.retrieve_procedures()
    assert any(e.key == good_key for e in entries), (
        f"key {good_key!r} did not round-trip via retrieve_procedures"
    )


async def test_store_procedure_refresh_path_revalidates_key(
    facade: MemoryFacade,
) -> None:
    """A re-store of an existing key still validates the key on the
    refresh path — a regression that bypassed the regex on the
    "key already exists" branch would silently let an invalid key
    through (the bug that PR 5 R2 M1 closes)."""
    await facade.store_procedure("good", "body", confidence=0.5)
    # An invalid key must be rejected even when the call would
    # otherwise hit the existing-row refresh path.
    with pytest.raises(ValueError, match="A-Za-z0-9._-"):
        await facade.store_procedure("bad key", "body", confidence=0.5)


async def test_store_procedure_idempotent_re_store_for_canonical_key(
    facade: MemoryFacade,
) -> None:
    """Re-storing a canonical key hits the refresh path without
    raising.  Pins that key validation does not break the documented
    refresh-on-reuse contract.
    """
    await facade.store_procedure("dep.deploy", "v1 body", confidence=1.0)
    # Forge a stale ``last_validated_at`` so the refresh effect is
    # observable as a fresh timestamp on the row.  We do not assert
    # the decayed-confidence value because the legacy-row
    # compatibility shim in :func:`resolve_base_confidence` would
    # surface ``importance`` (set on insert) rather than the post-
    # refresh ``confidence`` for non-1.0 authored values — that
    # interaction is pinned separately by the PR 5 review tests.
    db = facade.episodic._ensure_db()  # noqa: SLF001
    await db.execute(
        "UPDATE episodes SET last_validated_at = 0 WHERE agent_id = ?",
        ("proc-key-test",),
    )
    await db.commit()
    # Second store with the same key — must hit refresh + not raise.
    await facade.store_procedure("dep.deploy", "v1 body", confidence=1.0)
    async with db.execute(
        "SELECT last_validated_at FROM episodes WHERE agent_id = ?",
        ("proc-key-test",),
    ) as cur:
        rows = list(await cur.fetchall())
    assert all(row[0] > 0 for row in rows), (
        "refresh path must stamp last_validated_at on canonical-key re-store"
    )
