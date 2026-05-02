"""Data-driven task agent.

A single agent class that reads behavior from YAML ``instructions``
instead of hard-coding a system prompt per agent role.  Replaces the
v0.1 ``CoderAgent``, ``ReviewerAgent``, and ``PlannerAgent``.

RFC 0008 PR 3 (delegation contract): when invoked with a
:data:`agents.sub_agents.delegation.DELEGATION_REQUEST_KEY` entry in
``task.context``, the agent additionally emits a validated
:class:`agents.sub_agents.delegation.DelegationResult` under
:data:`agents.sub_agents.delegation.DELEGATION_RESULT_KEY` in
``TaskOutput.metadata`` so the caller's spawner can route the result
through :class:`agents.sub_agents.merge.MergeEngine`.
"""

from __future__ import annotations

import json
import logging

from .base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from .prompt_loader import load_snippet
from .sub_agents._log_safety import (
    bounded as _bounded,  # PR #224 r5-S1: CWE-117 (lifted to _log_safety in PR 6b)
)
from .sub_agents.delegation import (
    DELEGATION_REQUEST_KEY,
    DELEGATION_RESULT_KEY,
    DelegationContractError,
    DelegationResult,
)
from .tools import builtin

logger = logging.getLogger(__name__)


class TaskAgent(BaseAgent):
    """Executes tasks using instructions from YAML agent config.

    The system prompt is composed as::

        Role: {config.role}

        {config.instructions}      # may be empty

        Workspace root: /workspace  # injected when tools are available

    This makes new agent roles a config-only change — no Python code needed.
    """

    async def handle(self, task: TaskInput) -> TaskOutput:
        instructions = self.config.get("instructions", "")
        system_prompt = f"Role: {self.role}"
        if instructions:
            system_prompt = f"{system_prompt}\n\n{instructions}"
        # Inject workspace root so the LLM uses correct absolute paths when
        # calling file_read / file_write / shell_exec tools.
        if builtin.workspace_root is not None and self.config.get("tools"):
            workspace_instructions = load_snippet("workspace-root-instructions").format(
                workspace_root=builtin.workspace_root,
            )
            system_prompt = f"{system_prompt}\n\n{workspace_instructions}"
        output = await self._run_llm_loop(task, system_prompt=system_prompt)
        if DELEGATION_REQUEST_KEY in task.context:
            self._attach_delegation_result(output)
        return output

    # ─── RFC 0008 PR 3 — delegation result emission ──────────────

    def _attach_delegation_result(self, output: TaskOutput) -> None:
        """Synthesise a :class:`DelegationResult` and attach it to *output*.

        If the LLM happened to emit a valid :class:`DelegationResult`
        JSON payload as ``output.result``, we parse and re-validate it.
        Otherwise (the common case in v0.3) we synthesise a minimal
        envelope: ``summary = result``, ``status`` derived from
        :class:`TaskStatus`, no ``memory_writes``.  Sub-agent authors
        that need richer results override :meth:`TaskAgent.handle` and
        construct the envelope explicitly.
        """
        result = self._parse_or_synthesise(output)
        try:
            result.validate()
        except DelegationContractError as exc:
            logger.warning(
                "agent %s emitted invalid DelegationResult, "
                "falling back to synthesised envelope: %s",
                self.agent_id, exc,
            )
            result = self._synthesise(output)
            result.validate()
        output.metadata[DELEGATION_RESULT_KEY] = result.to_json()

    def _parse_or_synthesise(self, output: TaskOutput) -> DelegationResult:
        # PR #224 (RFC 0008 PR 3a) — N6: collapse the previous two-pass
        # heuristic.  PR 3 first pattern-matched ``text.startswith("{")
        # and text.endswith("}")``, then ran ``json.loads`` for type
        # inspection, then handed the same string to
        # :meth:`DelegationResult.from_metadata_value` which parses it
        # a third time — and noisily logged each intermediate failure
        # mode even though the only meaningful question is "did the
        # contract parser accept it?"
        #
        # Single-pass version: hand straight to the contract parser and
        # synthesise on either ``DelegationContractError`` or
        # ``json.JSONDecodeError`` (subclass of ``ValueError``).  The
        # contract parser already does the JSON decode, the dict-shape
        # check, and (post-S6) the closed-set ``status`` check.  We
        # debug-log the rejection so operators investigating a noisy
        # agent can still see why the synthesis path fired, but we do
        # not warn — the synthesis path is the documented common case
        # for v0.3.
        text = (output.result or "").strip()
        if not text:
            return self._synthesise(output)
        try:
            return DelegationResult.from_metadata_value(text)
        except (DelegationContractError, json.JSONDecodeError) as exc:
            # PR #224 review round-5 (Should #1): wrap ``exc`` in
            # ``_bounded`` to neutralise the same CWE-117 / OWASP A09
            # vector closed at the spawner-side ``DelegationFailure``
            # raise sites.  The exception text embeds the offending
            # JSON fragment (``json.JSONDecodeError`` echoes the input
            # via ``doc[pos-N:pos+N]``; ``DelegationContractError``
            # interpolates contract-violating values verbatim), so an
            # unbounded ``%s`` interpolation would render attacker-
            # influenceable control characters into the debug log.
            logger.debug(
                "agent %s output is not a DelegationResult payload "
                "(%s); synthesising envelope",
                self.agent_id, _bounded(exc),
            )
            return self._synthesise(output)

    def _synthesise(self, output: TaskOutput) -> DelegationResult:
        status = "completed" if output.status == TaskStatus.COMPLETED else "failed"
        return DelegationResult(
            summary=output.result or "",
            status=status,
        )
