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

import jsonschema  # type: ignore[import-untyped]

from ..base import CONTEXT_PACKAGE_KEY, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from ._log_safety import bounded as _bounded
from .delegation import (
    DELEGATION_REQUEST_KEY,
    DELEGATION_RESULT_KEY,
    MAX_CONTEXT_PACKAGE_BYTES,
    DelegationContractError,
    DelegationFailure,
    DelegationRequest,
    DelegationResult,
)
from .merge import MergeEngine, MergeOutcome

if TYPE_CHECKING:
    from ..base import BaseAgent

logger = logging.getLogger(__name__)

# PR 6b: log-safety helpers (``_bounded`` + constants) live in
# :mod:`._log_safety`; the underscore aliases imported above are
# kept module-local for the existing call sites.


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
        # PR #222 deep review S5: bound untrusted-shape input at the
        # spawner trust boundary (OWASP A05).  ``context_package`` is
        # typed ``dict[str, Any]`` with no depth or size limit — a
        # hostile or buggy caller could otherwise push an arbitrarily
        # large blob into ``task.context`` (which is ``dict[str, str]``)
        # and the sub-agent's memory.  We fail fast in the *caller*
        # stack rather than letting the sub-agent OOM.  The per-field
        # check stays ahead of the whole-payload check below so the
        # error message points at the offending field.
        serialised_pkg = ""
        if request.context_package:
            serialised_pkg = _json.dumps(
                request.context_package, sort_keys=True,
            )
            if len(serialised_pkg) > MAX_CONTEXT_PACKAGE_BYTES:
                raise DelegationContractError(
                    f"DelegationRequest.context_package exceeds size cap: "
                    f"{len(serialised_pkg)} > {MAX_CONTEXT_PACKAGE_BYTES} bytes",
                )
        # PR #224 (RFC 0008 PR 3a) — N7: serialise the request payload
        # exactly once.  PR 3 called ``request.to_json()`` for
        # ``DELEGATION_REQUEST_KEY`` and then re-serialised
        # ``output_schema`` immediately below purely to size-check it,
        # which both wasted CPU on every dispatch and risked the two
        # encodings drifting (``sort_keys`` etc.).  The whole-payload
        # cap subsumes the per-field cap because every constituent
        # (``context_package``, ``output_schema``, …) is a subset of
        # the serialised request.
        request_payload = request.to_json()
        if len(request_payload) > MAX_CONTEXT_PACKAGE_BYTES:
            raise DelegationContractError(
                f"DelegationRequest payload exceeds size cap: "
                f"{len(request_payload)} > {MAX_CONTEXT_PACKAGE_BYTES} bytes",
            )
        context: dict[str, str] = {
            DELEGATION_REQUEST_KEY: request_payload,
        }
        if serialised_pkg:
            context[CONTEXT_PACKAGE_KEY] = serialised_pkg

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
        # PR #224 (RFC 0008 PR 3a) — S1: enforce ``output_schema``
        # against ``DelegationResult.artifacts`` *before* the merge
        # engine runs.  PR 3 marked this an explicit TODO (OWASP A04
        # — improper input validation): callers populating
        # ``output_schema`` were silently treating it as advisory, and
        # a sub-agent that returned the wrong artifact shape would
        # surface only at the next consumer.  We fail at the trust
        # boundary instead.
        self._enforce_output_schema(request, result)
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

    @staticmethod
    def _enforce_output_schema(
        request: DelegationRequest,
        result: DelegationResult,
    ) -> None:
        """Validate ``result.artifacts`` against ``request.output_schema``.

        No-op when ``output_schema`` is empty (the contract treats an
        empty schema as "no constraint").  Surfaces a
        :class:`DelegationFailure` with the offending JSON-pointer-style
        path on any violation so callers can route the failure into a
        retry / human-review queue without having to re-serialise the
        artifacts.

        The schema itself is re-validated (Draft-7 meta-schema) on
        every dispatch — caller-supplied ``output_schema`` payloads in
        v0.3 are tiny (sub-agent reply contracts), so the cost is
        negligible and avoiding a cache keeps the spawner stateless.
        A malformed ``output_schema`` is a caller bug and surfaces the
        same way as a contract violation.

        PR #224 review (Should #1): the prior "validated only on first
        use" wording implied caching that the implementation does not
        provide — corrected to match behaviour.
        """
        if not request.output_schema:
            return
        try:
            jsonschema.Draft7Validator.check_schema(request.output_schema)
        except jsonschema.SchemaError as exc:
            raise DelegationFailure(
                f"DelegationRequest.output_schema is not a valid Draft-7 "
                f"schema: {_bounded(exc.message)}",
            ) from exc
        validator = jsonschema.Draft7Validator(request.output_schema)
        errors = sorted(
            validator.iter_errors(result.artifacts),
            key=lambda e: list(e.absolute_path),
        )
        if not errors:
            return
        first = errors[0]
        path = "/".join(str(p) for p in first.absolute_path) or "<root>"
        # PR #224 review (Should #4 / round-2 S2-mirror): ``jsonschema``'s
        # ``ValidationError.message`` embeds a ``repr()`` of the offending
        # instance fragment, so an unbounded message can echo sub-agent
        # ``artifacts`` content into logs (LLM01 / OWASP A09).  Surface the
        # structural fields (validator name + expected value) plus the
        # ``_bounded`` message tail so operators retain enough signal for
        # triage without leaking full payloads.  ``_bounded`` is the same
        # helper used at every other ``DelegationFailure`` raise site that
        # interpolates attacker-influenceable text.
        #
        # PR #224 review round-3 (Should #2): ``validator_value`` is also
        # bounded for symmetry — ``output_schema`` is caller-controlled
        # (workflow author), but the validator value can itself be a large
        # nested structure (e.g. a giant ``enum: [...]`` or deep ``oneOf:``
        # tree) whose ``repr()`` would otherwise flood logs without going
        # through the same cap that protects the message tail.  Same helper,
        # ``repr()`` first so structural details survive truncation.
        raise DelegationFailure(
            f"DelegationResult.artifacts violates output_schema at "
            f"{path}: validator={first.validator!r} "
            f"expected={_bounded(repr(first.validator_value))}: "
            f"{_bounded(first.message)}",
        )

    def _extract_result(self, output: TaskOutput) -> DelegationResult:
        """Deserialise :data:`DELEGATION_RESULT_KEY` from *output*."""
        # PR #224 review round-2 (S2-mirror): ``output.result`` and the
        # wrapped ``DelegationContractError`` text both carry
        # attacker-influenceable payloads (sub-agent reply content).
        # Funnel through ``_bounded`` so error messages cannot exfiltrate
        # arbitrary-length payloads into orchestrator logs.
        if output.status == TaskStatus.FAILED:
            raise DelegationFailure(
                f"sub-agent reported FAILED: {_bounded(output.result)}",
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
                f"sub-agent emitted invalid DelegationResult: {_bounded(exc)}",
            ) from exc

    async def _persist_admitted(self, outcome: MergeOutcome) -> list[str]:
        """Hook for subclasses to persist admitted entries.

        Base implementation is a no-op that returns the entry keys —
        callers can read :attr:`SpawnResult.outcome` and persist
        directly.  See :class:`FacadeBoundSpawner` for the bound-facade
        variant used in the integration tests.

        .. note::
           PR #222 deep review N4: the ``persist_to_memory`` flag on
           :meth:`SubAgentSpawner.dispatch` defaults to ``True`` for
           contract-symmetry with :class:`FacadeBoundSpawner` (where the
           default *does* persist).  On the base class the flag only
           controls whether this hook is invoked at all — and this
           hook is a no-op — so the default has no observable effect.
           Subclasses that perform real persistence inherit the
           caller-friendly ``True`` default.
        """
        return [entry.key for entry in outcome.admitted]


class FacadeBoundSpawner(SubAgentSpawner):
    """Spawner that persists admitted entries through a bound MemoryStore."""

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
        """Persist admitted entries through the bound facade.

        PR #224 (RFC 0008 PR 3a) — N5: best-effort rollback on
        partial-batch failure.  PR 3 persisted entries one-by-one and,
        on any ``store_observation`` exception, left a partially-
        persisted batch behind with no record of which keys had landed.
        Replay logic and the caller's :class:`SpawnResult.admitted_entry_ids`
        contract both assumed all-or-nothing semantics.

        We now compensate as follows:

        1. Persist each admitted entry, accumulating the returned IDs.
        2. On the first ``store_observation`` failure, delegate to
           :meth:`_rollback_persisted`, which calls
           ``self._facade.episodic.delete_episode(id)`` on every
           successfully-persisted entry from this batch in reverse
           order.  Each rollback is best-effort: we swallow individual
           exceptions so the original error remains the surfaced cause,
           and we log any rollback failures so the operator can reconcile.
        3. Re-raise the original exception with the original traceback.

        The facade's ``episodic`` accessor exposes ``delete_episode``
        (added in PR 3a alongside this rollback path); if a future
        facade variant lacks the accessor we degrade to a warning and
        skip rollback rather than masking the real error.

        PR #224 review (Must #1): prior wording referenced a non-existent
        ``facade.forget`` API — corrected to point at the actual
        ``episodic.delete_episode`` call site below.
        """
        ids: list[str] = []
        try:
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
        except Exception:
            await self._rollback_persisted(ids)
            raise
        return ids

    async def _rollback_persisted(self, ids: list[str]) -> None:
        """Best-effort reverse-order delete of partially-persisted IDs."""
        episodic = getattr(self._facade, "episodic", None)
        if episodic is None:
            logger.warning(
                "facade %r does not expose episodic accessor; skipping "
                "rollback of %d partially-persisted entries",
                type(self._facade).__name__, len(ids),
            )
            return
        # TODO(RFC 0008 PR 5): ``_persist_admitted`` currently routes
        # both ``episodic`` and ``notes`` tier writes through
        # ``store_observation`` (which lands them in episodic storage),
        # so a single ``delete_episode`` rollback is sufficient.  When
        # PR 5 splits the notes tier to a separate persistence path,
        # this rollback must dispatch by ``entry.tier`` rather than
        # calling ``delete_episode`` unconditionally — otherwise notes-
        # tier rollbacks will silently no-op.  Flagged by PR #224 review
        # (Should #2) as a forward-compat hazard.
        for entry_id in reversed(ids):
            try:
                await episodic.delete_episode(entry_id)
            except Exception:
                logger.warning(
                    "rollback of partially-persisted entry %s failed; "
                    "operator must reconcile",
                    entry_id, exc_info=True,
                )


__all__ = [
    "FacadeBoundSpawner",
    "SpawnResult",
    "SubAgentSpawner",
]
