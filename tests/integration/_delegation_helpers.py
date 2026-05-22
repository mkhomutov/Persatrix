"""Shared sub-agent test helpers — RFC 0008 PR 6b consolidation.

Lifts the three near-duplicate ``_ScriptedSubAgent`` / ``_FailedSubAgent``
/ ``_MalformedSubAgent`` definitions previously copy-pasted across
:mod:`tests.integration.test_delegation_end_to_end` and
:mod:`tests.integration.test_delegation_rollback_edges` into a single
shared module.  Closes the [PR 3a R2 L2 / R4 L5 follow-up](
../../docs/rfcs/0008-pr-plan.md#consolidated-triage-table).

The helpers stay deliberately minimal so they remain test-only fixtures
— production sub-agent code lives in :mod:`agents.task_agent`.
"""

from __future__ import annotations

from typing import Any

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.sub_agents import (
    DELEGATION_REQUEST_KEY,
    DELEGATION_RESULT_KEY,
    DelegationResult,
)


class ScriptedSubAgent(BaseAgent):
    """Minimal sub-agent that returns a pre-canned :class:`DelegationResult`.

    Bypasses the LLM loop so integration tests exercise only the
    delegation contract + merge engine + memory persistence path.
    Asserts the request rode in on the reserved
    :data:`DELEGATION_REQUEST_KEY` so a regression dropping the wire
    contract surfaces here rather than in a far-away assertion.
    """

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


class FailedSubAgent(BaseAgent):
    """Sub-agent that returns ``FAILED`` with a configurable result string.

    Used by tests pinning the spawner's :class:`DelegationFailure`
    message-bound + control-character-strip behaviour
    (``test_delegation_rollback_edges``'s S2-mirror suite).
    """

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


class MalformedSubAgent(BaseAgent):
    """Sub-agent that returns a :class:`TaskOutput` without the reserved
    metadata key — the spawner must surface this as a contract error.
    """

    def __init__(self, agent_id: str = "malformed") -> None:
        super().__init__(agent_id=agent_id, config={})

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="raw text")


async def boom_delete(*_args: Any, **_kwargs: Any) -> bool:
    """Test double for :meth:`MemoryStore.episodic.delete_episode`
    that always raises — used to exercise the rollback-failure path.

    PR 3a R2 L3: takes ``*_args, **_kwargs`` for forward-compat with any
    future signature change to ``delete_episode`` (e.g. an added
    ``hard_delete=`` kwarg).  Tests passing this double via
    :meth:`pytest.MonkeyPatch.setattr` therefore do not need to be
    revisited if the production signature grows.
    """
    raise RuntimeError("simulated delete_episode failure")


__all__ = [
    "ScriptedSubAgent",
    "FailedSubAgent",
    "MalformedSubAgent",
    "boom_delete",
]
