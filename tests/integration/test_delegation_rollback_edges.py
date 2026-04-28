"""Edge-case coverage for the FacadeBoundSpawner rollback path.

Split out from ``test_delegation_end_to_end.py`` during PR #224 review
fix-up: the parent file hit the 500-line review-friendliness limit
(see ``scripts/checks/file_size.py``) when the two N5 coverage tests
landed.  These tests are conceptually distinct from the round-trip
end-to-end suite — they assert *failure-mode* behaviour of the
rollback compensator added in RFC 0008 PR 3a — so a dedicated file
keeps both suites focused.

PR #224 review (Should #3) closes the two N5 gaps:
    (a) rollback-during-rollback must not mask the original cause;
    (b) a facade variant without an ``episodic`` accessor must
        degrade to a warning rather than crashing dispatch.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.memory import MemoryFacade
from agents.sub_agents import (
    DELEGATION_REQUEST_KEY,
    DELEGATION_RESULT_KEY,
    DelegationFailure,
    DelegationRequest,
    DelegationResult,
    FacadeBoundSpawner,
    MemoryWriteEntry,
)


class _ScriptedSubAgent(BaseAgent):
    """Pre-canned sub-agent — duplicated from the end-to-end suite to
    keep this file self-contained (the original is module-private)."""

    def __init__(self, agent_id: str, result: DelegationResult) -> None:
        super().__init__(agent_id=agent_id, config={})
        self._result = result

    async def handle(self, task: TaskInput) -> TaskOutput:
        assert DELEGATION_REQUEST_KEY in task.context
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result=self._result.summary,
            metadata={DELEGATION_RESULT_KEY: self._result.to_json()},
        )


@pytest.fixture
async def parent_facade(tmp_path: Any) -> AsyncGenerator[MemoryFacade, None]:
    facade = MemoryFacade(
        agent_id="parent-coordinator", db_path=str(tmp_path / "parent.db"),
    )
    await facade.initialize()
    try:
        yield facade
    finally:
        await facade.close()


@pytest.mark.asyncio
async def test_rollback_failure_does_not_mask_original_cause(
    parent_facade: MemoryFacade,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N5 coverage: when ``delete_episode`` itself raises during
    rollback, the original ``store_observation`` failure must still be
    the surfaced exception (rollback errors are logged, not re-raised).

    PR #224 review round-5 (Should #2): switched from manual
    ``parent_facade.store_observation = flaky_store`` assignment to
    the ``monkeypatch`` fixture so teardown always restores the
    original attribute even if an inner assertion fails before the
    explicit restore line.  Also drops the ``# type: ignore[method-
    assign]`` suppression that the manual assignment required.
    """
    real_store = parent_facade.store_observation
    call_count = {"n": 0}

    async def flaky_store(*args: Any, **kwargs: Any) -> str:
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated store failure")
        return await real_store(*args, **kwargs)

    monkeypatch.setattr(parent_facade, "store_observation", flaky_store)

    # Force every rollback delete to raise.
    async def boom_delete(_entry_id: str) -> bool:
        raise RuntimeError("simulated delete_episode failure")

    monkeypatch.setattr(
        parent_facade.episodic, "delete_episode", boom_delete,
    )

    canned = DelegationResult(
        summary="batch",
        status="completed",
        memory_writes=tuple(
            MemoryWriteEntry(tier="episodic", key=f"k{i}", content=f"c{i}")
            for i in range(5)
        ),
    )
    child = _ScriptedSubAgent("batcher-rb-fail", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="batch persist (rollback fails)")

    # The original store failure must still be the surfaced exception —
    # the delete_episode failure is intentionally swallowed.
    with pytest.raises(RuntimeError, match="simulated store failure"):
        await spawner.dispatch(child, req)


@pytest.mark.asyncio
async def test_rollback_skipped_when_facade_lacks_episodic_accessor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N5 coverage: a facade variant without an ``episodic`` accessor
    must NOT crash the dispatch path.  The spawner logs a warning and
    re-raises the original cause (no rollback attempted).

    PR #224 review round-2 (S3-caplog) — also pin the documented
    warning log.  Without the ``caplog`` assertion a regression
    silently dropping the warning would still pass.
    """

    class _NoEpisodicFacade:
        """Minimal facade stub exposing only ``store_observation``."""

        def __init__(self) -> None:
            self.calls = 0

        async def store_observation(
            self, content: str, **_kwargs: Any,
        ) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated store failure")
            return f"stub-id-{self.calls}"

    facade = _NoEpisodicFacade()
    canned = DelegationResult(
        summary="batch",
        status="completed",
        memory_writes=(
            MemoryWriteEntry(tier="episodic", key="k0", content="c0"),
            MemoryWriteEntry(tier="episodic", key="k1", content="c1"),
        ),
    )
    child = _ScriptedSubAgent("batcher-no-episodic", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=facade,
    )
    req = DelegationRequest(objective="batch persist (no episodic)")

    with caplog.at_level("WARNING", logger="agents.sub_agents.spawner"):
        with pytest.raises(RuntimeError, match="simulated store failure"):
            await spawner.dispatch(child, req)

    assert "does not expose episodic accessor" in caplog.text


# ─── PR #224 round-2 (S2-mirror) — DelegationFailure message bound ─


class _FailedSubAgent(BaseAgent):
    """Sub-agent that returns FAILED with a configurable result string."""

    def __init__(self, agent_id: str, payload: str) -> None:
        super().__init__(agent_id=agent_id, config={})
        self._payload = payload

    async def handle(self, task: TaskInput) -> TaskOutput:
        assert DELEGATION_REQUEST_KEY in task.context
        return TaskOutput(
            status=TaskStatus.FAILED,
            result=self._payload,
            metadata={},
        )


@pytest.mark.asyncio
async def test_failed_status_payload_is_truncated_in_failure_message(
    parent_facade: MemoryFacade,
) -> None:
    """S2-mirror: when a sub-agent returns FAILED, the spawner's
    :class:`DelegationFailure` message must funnel ``output.result``
    through ``_bounded`` so attacker-influenceable text cannot
    exfiltrate arbitrary-length payloads into orchestrator logs
    (LLM01 / OWASP A09 log-injection).  Pin the canonical
    ``… (truncated)`` marker so log-grep tooling continues to work.
    """
    huge = "X" * 5000  # well above the 200-char cap
    child = _FailedSubAgent("failer", huge)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="provoke failure")

    with pytest.raises(DelegationFailure) as excinfo:
        await spawner.dispatch(child, req)

    msg = str(excinfo.value)
    assert "… (truncated)" in msg, (
        "DelegationFailure message must use the canonical truncation marker"
    )
    # PR #224 round-3 (Should #3): pin the documented 200-char cap.  The
    # round-2 test only asserted the marker plus a 1 KB belt-and-braces
    # bound — a regression that bumped ``_DELEGATION_FAILURE_MESSAGE_CAP``
    # from 200 to e.g. 2000 would still pass.  Assert the exact contract:
    # exactly 200 ``X`` characters from the payload land in the message,
    # and the 201st does not.  Both assertions together catch any drift
    # of the cap constant in either direction.
    assert "X" * 200 in msg, (
        "DelegationFailure message must include the full 200-char cap "
        "from the bounded payload"
    )
    assert "X" * 201 not in msg, (
        "DelegationFailure message exceeded the documented 200-char cap"
    )
    # Cap is 200 chars of payload; full prefix + cap + marker is well
    # under 1 KB.  Belt-and-braces upper bound to catch catastrophic
    # regressions where the cap is removed entirely.
    assert len(msg) < 1024, f"DelegationFailure message exceeded sane cap: {len(msg)}"


@pytest.mark.asyncio
async def test_failed_status_payload_control_chars_are_stripped(
    parent_facade: MemoryFacade,
) -> None:
    """PR #224 review round-4 (Should #1): pin the CWE-117 control-char
    strip in ``_bounded``.

    Round-3 added ``s.translate(_CTRL_TRANSLATION)`` in
    ``agents/sub_agents/spawner.py`` to neutralise forged-line
    injection — a sub-agent that returns
    ``output.result = "harmless\\n[ERROR] forged admin alert"`` (well
    under the 200-char volume cap) would otherwise inject a fake log
    line into the ``DelegationFailure`` message that downstream
    ``logger.error("dispatch failed: %s", exc)`` calls render verbatim
    across the newline.  The existing truncation test uses a payload
    with no control characters, so a regression dropping the strip
    would silently pass.  This test pins the strip from both
    directions: the U+2424 sentinel must appear, *and* the original
    control characters must be absent.
    """
    # Mix of the most dangerous control characters: \n / \r (forged-line
    # injection), \x1b (ANSI escape sequence — terminal hijack), \x00
    # (NUL — log-pipeline truncation), \t (TSV column injection).
    payload = "harmless\n[ADMIN] forged\r\x1b[31mred\x00trail\there"
    child = _FailedSubAgent("ctrl-injector", payload)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="provoke ctrl-char failure")

    with pytest.raises(DelegationFailure) as excinfo:
        await spawner.dispatch(child, req)

    msg = str(excinfo.value)
    # Sentinel must appear at least once for every stripped codepoint
    # in the payload (5 control chars: \n \r \x1b \x00 \t).
    assert msg.count("\u2424") >= 5, (
        "DelegationFailure message must replace each control character "
        f"with the U+2424 sentinel; got: {msg!r}"
    )
    # No raw control characters from the payload may survive into the
    # rendered failure message.  Asserting per-character so a regression
    # report points at the offending codepoint.
    for ctrl in ("\n", "\r", "\x1b", "\x00", "\t"):
        assert ctrl not in msg, (
            f"DelegationFailure message leaked raw control char {ctrl!r}"
        )
    # The visible payload prefix ("harmless") must still be present —
    # the strip must not nuke surrounding text.  Operator-triage signal
    # is the whole point of bounding rather than redacting.
    assert "harmless" in msg
    assert "forged" in msg
