"""
Sub-agent spawner — contract-aware in-process dispatch (RFC 0008 PR 3).

PR 3 of the [RFC 0008 PR plan](../../docs/rfcs/0008-pr-plan.md) replaces
the previous v0.2 TODO stub with a minimal in-process spawner that wraps
the existing :meth:`agents.base.BaseAgent.handle` dispatch path with the
:class:`agents.sub_agents.delegation.DelegationRequest` /
:class:`agents.sub_agents.delegation.DelegationResult` contract and routes
the result through :class:`agents.sub_agents.merge.MergeEngine`.

Out of scope (deferred to RFC 0009)
-----------------------------------
* permission inheritance validation (child ≤ parent)
* depth / concurrency limit enforcement
* process-level isolation
* budget cascading from a parent pool

The minimal spawner here is deliberately synchronous-with-asyncio: it
calls the child agent's ``handle`` coroutine in-process so PR 3 can be
fully exercised in unit + integration tests without standing up a
gRPC sub-process.  Wire-level dispatch lands in RFC 0009.
"""

from __future__ import annotations

import json as _json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..base import CONTEXT_PACKAGE_KEY, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from .delegation import (
    DELEGATION_REQUEST_KEY,
    DELEGATION_RESULT_KEY,
    DelegationContractError,
    DelegationFailure,
    DelegationRequest,
    DelegationResult,
)
from .merge import MergeEngine, MergeOutcome

if TYPE_CHECKING:
    from ..base import BaseAgent

logger = logging.getLogger(__name__)


@dataclass
class SpawnResult:
    """Bundle returned by :meth:`SubAgentSpawner.dispatch`.

    ``result`` is the deserialised :class:`DelegationResult`.  ``outcome``
    is the merge engine's per-entry decision record (admitted /
    rejected).  ``raw_output`` is the underlying :class:`TaskOutput` for
    callers that need the unstructured agent reply (e.g. for logging).
    """

    result: DelegationResult
    outcome: MergeOutcome
    raw_output: TaskOutput
    admitted_entry_ids: list[str] = field(default_factory=list)


class SubAgentSpawner:
    """Wraps the existing dispatch path with the RFC 0008 §E contract."""

    def __init__(
        self,
        parent_agent_id: str,
        *,
        merge_engine: MergeEngine | None = None,
    ) -> None:
        if not parent_agent_id or not parent_agent_id.strip():
            raise ValueError("parent_agent_id must not be empty")
        self._parent_agent_id = parent_agent_id
        self._merge_engine = merge_engine or MergeEngine()

    async def dispatch(
        self,
        child: BaseAgent,
        request: DelegationRequest,
        *,
        workflow_id: str | None = None,
        existing_artifacts: dict[str, Any] | None = None,
        existing_keys: list[str] | None = None,
        persist_to_memory: bool = True,
    ) -> SpawnResult:
        """Send *request* to *child* and merge the result.

        The spawner:

        1. Validates the request (caller-side stack trace on contract violations).
        2. Serialises the request into ``TaskInput.context`` under
           :data:`DELEGATION_REQUEST_KEY`.
        3. Awaits ``child.handle(task)``.
        4. Reads :data:`DELEGATION_RESULT_KEY` from ``TaskOutput.metadata`` and
           deserialises it.
        5. Runs :meth:`MergeEngine.merge_result` with
           ``source_agent=child.agent_id``.
        6. Optionally persists admitted ``memory_writes`` (subclass hook).
        """
        request.validate()

        task_id = f"delegation-{uuid.uuid4().hex[:12]}"
        wf_id = workflow_id or "delegation"
        context: dict[str, str] = {
            DELEGATION_REQUEST_KEY: request.to_json(),
        }
        if request.context_package:
            context[CONTEXT_PACKAGE_KEY] = _json.dumps(
                request.context_package, sort_keys=True,
            )

        task = TaskInput(
            task_id=task_id,
            workflow_id=wf_id,
            payload=request.objective,
            context=context,
            config=TaskInputConfig(
                max_llm_calls=request.budget.max_llm_calls,
                max_tokens=request.budget.tokens,
                allowed_tools=sorted(request.allowed_tools),
            ),
        )

        output = await child.handle(task)

        result = self._extract_result(output)
        outcome = self._merge_engine.merge_result(
            result,
            request,
            source_agent=child.agent_id,
            existing_artifacts=existing_artifacts,
            existing_keys=existing_keys or [],
        )

        admitted_ids: list[str] = []
        if persist_to_memory and outcome.admitted:
            admitted_ids = await self._persist_admitted(outcome)

        return SpawnResult(
            result=result,
            outcome=outcome,
            raw_output=output,
            admitted_entry_ids=admitted_ids,
        )

    # -- internals --------------------------------------------------

    def _extract_result(self, output: TaskOutput) -> DelegationResult:
        """Deserialise :data:`DELEGATION_RESULT_KEY` from *output*."""
        if output.status == TaskStatus.FAILED:
            raise DelegationFailure(
                f"sub-agent reported FAILED: {output.result}",
            )
        raw = output.metadata.get(DELEGATION_RESULT_KEY)
        if raw is None:
            raise DelegationFailure(
                f"sub-agent did not emit {DELEGATION_RESULT_KEY!r} in "
                "TaskOutput.metadata — contract violation",
            )
        if not isinstance(raw, str):
            raise DelegationFailure(
                f"{DELEGATION_RESULT_KEY!r} metadata must be a JSON string, "
                f"got {type(raw).__name__}",
            )
        try:
            return DelegationResult.from_metadata_value(raw)
        except DelegationContractError as exc:
            raise DelegationFailure(
                f"sub-agent emitted invalid DelegationResult: {exc}",
            ) from exc

    async def _persist_admitted(self, outcome: MergeOutcome) -> list[str]:
        """Hook for subclasses to persist admitted entries.

        Base implementation is a no-op that returns the entry keys —
        callers can read :attr:`SpawnResult.outcome` and persist
        directly.  See :class:`FacadeBoundSpawner` for the bound-facade
        variant used in the integration tests.
        """
        return [entry.key for entry in outcome.admitted]


class FacadeBoundSpawner(SubAgentSpawner):
    """Spawner that persists admitted entries through a bound MemoryFacade."""

    def __init__(
        self,
        parent_agent_id: str,
        memory_facade: Any,
        *,
        merge_engine: MergeEngine | None = None,
    ) -> None:
        super().__init__(parent_agent_id, merge_engine=merge_engine)
        self._facade = memory_facade

    async def _persist_admitted(self, outcome: MergeOutcome) -> list[str]:
        ids: list[str] = []
        for entry in outcome.admitted:
            # Phase 2 memory facade routes both `episodic` and `notes`
            # writes through store_observation tagged with the tier
            # name.  PR 5 introduces a tier-discriminated path.
            tags = list(entry.tags) + [
                f"tier:{entry.tier}",
                f"key:{entry.key}",
                f"source:{entry.source_agent or 'unknown'}",
            ]
            entry_id = await self._facade.store_observation(
                entry.content,
                importance=entry.importance,
                ttl_seconds=entry.ttl_seconds,
                tags=tags,
            )
            ids.append(entry_id)
        return ids


__all__ = [
    "FacadeBoundSpawner",
    "SpawnResult",
    "SubAgentSpawner",
]
