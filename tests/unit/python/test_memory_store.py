"""Unit tests for ``agents.memory.store.MemoryStore`` (RFC 0029 Phase 1).

Phase 1 promoted the RFC 0008 ``MemoryFacade`` to the typed
``MemoryStore`` facade.  The promotion was a pure refactor — personal-tier
behaviour is identical and the ``test_memory_facade*.py`` suites pin the
API surface.  The one-minor-version ``MemoryFacade`` compatibility alias
was removed in v0.3.3; every suite imports ``MemoryStore`` directly.  This
file covers the new surface Phase 1 added:

- ``MemoryStore`` is the canonical class, exported from both
  :mod:`agents.memory` and :mod:`agents.memory.store`.
- Personal-tier calls (``store_observation`` → ``retrieve_relevant``,
  ``compress``) behave identically on ``MemoryStore``.
- Society-tier methods raise the ``SocietyBackendUnavailable`` hierarchy:
  ``SocietyDisabled`` in single-agent mode (``society_dsn=None``),
  ``SocietyTransientError`` when a DSN is set (no backend until Phase 3).
- ``record_action`` raises ``NotImplementedError`` — the SA-7 / RFC 0028
  audit backend is not chosen in Phase 1 (RFC 0029 §C).
- Single-agent mode opens no Postgres connection (no ``asyncpg`` import).
- ``StoreConfig`` is the frozen v0.4.0-boundary construction contract.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator

import pytest

from agents.memory.store import (
    MemoryDisabledError,
    MemoryEntry,
    MemoryStore,
    SocietyBackendUnavailable,
    SocietyDisabled,
    SocietyTransientError,
    StoreConfig,
)


@pytest.fixture
async def store() -> AsyncGenerator[MemoryStore, None]:
    """An initialised in-memory single-agent ``MemoryStore``."""
    st = MemoryStore(agent_id="store-test", db_path=":memory:")
    await st.initialize()
    try:
        yield st
    finally:
        await st.close()


# ─── MemoryStore package export ───────────────────────────────


def test_memory_store_exported_from_package() -> None:
    from agents.memory import MemoryStore as PackageMemoryStore
    from agents.memory import StoreConfig as PackageStoreConfig

    assert PackageMemoryStore is MemoryStore
    assert PackageStoreConfig is StoreConfig


# ─── Personal tier — behaviour identical to the legacy facade ──


async def test_store_observation_round_trips_through_retrieve_relevant(
    store: MemoryStore,
) -> None:
    entry_id = await store.store_observation(
        "the user uses Python type hints in 3.12 syntax",
        importance=0.9,
        tags=("python", "syntax"),
    )
    assert isinstance(entry_id, str) and entry_id
    results = await store.retrieve_relevant("python type hints", limit=5)
    assert any("type hints" in entry.content for entry in results)
    assert all(isinstance(entry, MemoryEntry) for entry in results)


async def test_personal_tier_use_before_initialize_raises(store: MemoryStore) -> None:
    cold = MemoryStore(agent_id="cold", db_path=":memory:")
    with pytest.raises(MemoryDisabledError, match="not initialised"):
        await cold.retrieve_relevant("anything")


async def test_compress_is_available_on_memory_store() -> None:
    entries = [
        MemoryEntry(
            id="a", content="alpha", importance=0.9,
            tags=(), created_at=0.0, score=0.0,
        ),
    ]
    view = MemoryStore.compress(entries, target_tokens=1000)
    assert "alpha" in view.summary
    assert view.entries_dropped == 0


# ─── Society tier — single-agent mode (society_dsn=None) ───────


def test_society_disabled_is_a_society_backend_unavailable() -> None:
    assert issubclass(SocietyDisabled, SocietyBackendUnavailable)
    assert issubclass(SocietyTransientError, SocietyBackendUnavailable)


async def test_read_pool_raises_society_disabled_in_single_agent_mode() -> None:
    st = MemoryStore(agent_id="alice", db_path=":memory:", society_dsn=None)
    with pytest.raises(SocietyDisabled):
        await st.read_pool("design-notes")


async def test_query_inbound_trust_raises_society_disabled_in_single_agent_mode() -> None:
    st = MemoryStore(agent_id="alice", db_path=":memory:", society_dsn=None)
    with pytest.raises(SocietyDisabled):
        await st.query_inbound_trust()


async def test_society_disabled_message_names_the_config_key() -> None:
    """The error tells the operator which config knob enables the tier."""
    st = MemoryStore(agent_id="alice", db_path=":memory:", society_dsn=None)
    with pytest.raises(SocietyBackendUnavailable, match=r"memory\.society"):
        await st.read_pool("design-notes")


# ─── Society tier — DSN configured, no Phase-1 backend ─────────


async def test_society_call_with_dsn_set_raises_transient_error() -> None:
    """A configured DSN is accepted by the schema but ignored in Phase 1.

    There is no Postgres backend until RFC 0029 Phase 3, so a society
    call surfaces as a transient (connectivity) failure, distinct from
    the intentional single-agent ``SocietyDisabled``.
    """
    st = MemoryStore(
        agent_id="alice", db_path=":memory:",
        society_dsn="postgres://localhost/society",
    )
    with pytest.raises(SocietyTransientError):
        await st.read_pool("design-notes")


# ─── record_action — reserved, backend not chosen in Phase 1 ───


async def test_record_action_raises_not_implemented(store: MemoryStore) -> None:
    with pytest.raises(NotImplementedError, match="record_action"):
        await store.record_action("observed-a-thing")


# ─── Single-agent mode opens no Postgres connection ────────────


async def test_single_agent_mode_opens_no_postgres() -> None:
    """RFC 0029 §Test Strategy ``test_single_agent_no_postgres``.

    ``MemoryStore`` with ``society_dsn=None`` runs the whole personal
    tier without importing ``asyncpg`` — Phase 1 adds no Postgres
    dependency and single-agent mode never opens a society connection.
    """
    st = MemoryStore(agent_id="alice", db_path=":memory:", society_dsn=None)
    await st.initialize()
    try:
        await st.store_observation("a personal-tier write", importance=0.5)
        results = await st.retrieve_relevant("personal-tier", limit=5)
        assert results
    finally:
        await st.close()
    assert "asyncpg" not in sys.modules


# ─── StoreConfig — frozen v0.4.0-boundary construction contract ─


def test_store_config_is_frozen_with_rfc_fields() -> None:
    cfg = StoreConfig(agent_id="alice")
    assert cfg.agent_id == "alice"
    assert cfg.society_dsn is None
    assert cfg.capability_token is None
    with pytest.raises((AttributeError, TypeError)):
        cfg.agent_id = "bob"  # type: ignore[misc]


async def test_memory_store_from_config_single_agent() -> None:
    cfg = StoreConfig(
        agent_id="alice", personal_db_path=":memory:", society_dsn=None,
    )
    st = MemoryStore.from_config(cfg)
    await st.initialize()
    try:
        assert st.agent_id == "alice"
        await st.store_observation("from-config write", importance=0.5)
        with pytest.raises(SocietyDisabled):
            await st.read_pool("design-notes")
    finally:
        await st.close()
