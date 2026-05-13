"""
Tests for RFC 0031 Phase 1 persona-runtime ``PERSATRIX_SESSION_ID`` threading.

``_LLMPersonaAgent`` reads the env var at construction time and stamps the
resolved value on every ``EpisodicMemory.store_episode`` /
``RelationshipMemory.record_interaction`` call it makes.  Unset env →
``"legacy"`` carve-out (mirrors the orchestrator-side default pinned in
PR 2 by ``cmd/orchestrator/startup.go::resolveSessionID``).

Phase 1 ships no recall-side filtering — these tests assert the write
contract only.  See ``docs/rfcs/0031-pr-plan.md`` PR 3 for scope.
"""

from __future__ import annotations

import logging

import pytest

from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


# ─── Constructor reads env var ──────────────────────────────


class TestSessionIDResolution:
    async def test_unset_defaults_to_legacy(self, monkeypatch):
        monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        assert agent._session_id == "legacy"

    async def test_set_value_round_trips(self, monkeypatch):
        monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        assert agent._session_id == "run-a"

    async def test_empty_string_defaults_to_legacy(self, monkeypatch):
        # An empty env var (set but blank — e.g. ``PERSATRIX_SESSION_ID=``
        # in a shell rc) must resolve the same way as an unset var so the
        # operator's mental model is "any falsy → legacy" rather than
        # surfacing two distinct empty-id rows in storage.
        monkeypatch.setenv("PERSATRIX_SESSION_ID", "")
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        assert agent._session_id == "legacy"

    async def test_unset_emits_info_log(self, monkeypatch, caplog):
        # Mirrors the Go side `resolveSessionID` INFO line so an operator
        # greps for the same canonical string across the two binaries.
        # ``PersonaAgent.__init__`` logs against the ``agents.persona``
        # logger, so the caplog scope covers the parent namespace which
        # captures both ``agents.persona`` and ``agents.persona_runtime``.
        monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
        with caplog.at_level(logging.INFO, logger="agents"):
            create_persona_agent(
                agent_id="ember-owl",
                config=_PERSONA_CONFIG,
                llm_client=_make_client(),
            )
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "PERSATRIX_SESSION_ID" in m and "legacy" in m for m in msgs
        ), f"expected boot-log line about PERSATRIX_SESSION_ID; got: {msgs!r}"

    async def test_set_value_does_not_log(self, monkeypatch, caplog):
        # Happy path should be silent; the boot logs are already noisy.
        monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
        with caplog.at_level(logging.INFO, logger="agents"):
            create_persona_agent(
                agent_id="ember-owl",
                config=_PERSONA_CONFIG,
                llm_client=_make_client(),
            )
        msgs = [r.getMessage() for r in caplog.records]
        assert not any(
            "PERSATRIX_SESSION_ID" in m for m in msgs
        ), f"happy path should not log; got: {msgs!r}"


# ─── Threading: store_episode call sites pick up session id ─


class TestSessionIDThreadsToStoreEpisode:
    async def test_single_turn_path_stamps_session_id(self, monkeypatch):
        # ``_store_event_episode`` with a TICK event takes the single-turn
        # path: open + structural-close on the tracker, then a single
        # ``store_episode`` call at the closed-interaction shape.  This
        # is the simplest call site to exercise — multi-turn and the
        # legacy-fallback paths share the same threading hook, so a
        # pass here proves the constructor wired ``session_id`` onto the
        # call surface.
        monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        try:
            captured: list[dict] = []
            original = agent._episodic_memory.store_episode

            async def spy(*args, **kwargs):
                captured.append(kwargs.copy())
                return await original(*args, **kwargs)

            agent._episodic_memory.store_episode = spy  # type: ignore[method-assign]

            await agent._store_event_episode(
                event=AgentEvent(event_type=EventType.TICK),
                actions=[],
            )

            assert captured, "expected store_episode to be called"
            assert all(c.get("session_id") == "run-a" for c in captured), (
                f"every store_episode call must carry session_id=run-a; "
                f"got: {captured!r}"
            )
        finally:
            await agent.close_memory()

    async def test_default_legacy_when_env_unset(self, monkeypatch):
        # Confirms the unset-env path round-trips ``"legacy"`` onto the
        # write — operator-visible carve-out parity with the orchestrator.
        monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        try:
            captured: list[dict] = []
            original = agent._episodic_memory.store_episode

            async def spy(*args, **kwargs):
                captured.append(kwargs.copy())
                return await original(*args, **kwargs)

            agent._episodic_memory.store_episode = spy  # type: ignore[method-assign]

            await agent._store_event_episode(
                event=AgentEvent(event_type=EventType.TICK),
                actions=[],
            )

            assert captured
            assert all(c.get("session_id") == "legacy" for c in captured)
        finally:
            await agent.close_memory()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
