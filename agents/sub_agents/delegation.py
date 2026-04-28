"""
Delegation contract — request / result envelopes for sub-agent dispatch (RFC 0008 §E).

PR 3 of the [RFC 0008 PR plan](../../docs/rfcs/0008-pr-plan.md) introduces the
typed contract that callers (task agents acting as parents) hand to sub-agents
and that sub-agents return.  The merge engine in
:mod:`agents.sub_agents.merge` consumes :class:`DelegationResult` and applies
the per-entry merge strategy against the caller's :class:`MemoryFacade`.

The procedural tier is intentionally excluded from
:class:`MemoryWriteEntry` — see [RFC 0008 PR plan](../../docs/rfcs/0008-pr-plan.md)
PR 3 *Key implementation details* for rationale (sub-agent trust ceiling
sits below the procedural ``c_min`` operating range).

Reserved ``TaskInput.context`` key
----------------------------------
The orchestrator (or in-process spawner) serialises the request as JSON
under :data:`DELEGATION_REQUEST_KEY` so sub-agents can deserialise via
:meth:`DelegationRequest.from_context_value`.  Mirrors the
``_context_package`` reserved-key pattern from PR 1.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)


# ─── Reserved context keys ──────────────────────────────────────

DELEGATION_REQUEST_KEY: Final[str] = "_delegation_request"
"""Reserved ``TaskInput.context`` key carrying a JSON-serialised
:class:`DelegationRequest`.  Mirrors the ``_context_package`` reservation
from PR 1 — picked deliberately so the orchestrator's existing
context-map plumbing does not need proto changes."""

DELEGATION_RESULT_KEY: Final[str] = "_delegation_result"
"""Reserved ``TaskOutput.metadata`` key carrying a JSON-serialised
:class:`DelegationResult` when a task agent runs as a sub-agent.  The
caller's spawner reads this key and routes the result through the merge
engine."""


# ─── Errors ─────────────────────────────────────────────────────


class DelegationContractError(ValueError):
    """Raised when a delegation request or result violates the schema.

    Subclasses :class:`ValueError` so existing ``try / except ValueError``
    paths in agent code continue to catch it."""


class DelegationFailure(RuntimeError):  # noqa: N818 — kept for RFC 0008 §E vocabulary
    """Raised when a sub-agent dispatch fails outright (no merge attempted).

    The merge engine raises this when the entire result is rejected
    (e.g. ``schema_invalid`` at step 1 of the deterministic merge order).
    Per-entry rejections are not fatal — they are logged and metrics are
    emitted but the surviving entries still merge.
    """


# ─── Allowed enums ──────────────────────────────────────────────

# Procedural tier intentionally excluded — see module docstring + PR 3 plan.
DelegationTier = Literal["episodic", "notes"]
DelegationStatus = Literal["completed", "partial", "failed"]
MergeStrategy = Literal["replace", "append", "patch", "reject_on_conflict"]

_ALLOWED_TIERS: Final[frozenset[str]] = frozenset({"episodic", "notes"})
_ALLOWED_STATUSES: Final[frozenset[str]] = frozenset({"completed", "partial", "failed"})
_ALLOWED_STRATEGIES: Final[frozenset[str]] = frozenset(
    {"replace", "append", "patch", "reject_on_conflict"},
)

DEFAULT_TRUST_CEILING: Final[float] = 0.8
"""Per RFC 0008 §E (``docs/rfcs/0008-agent-memory-context-optimization.md``):
unverified sub-agents cannot exceed this importance ceiling on any
admitted :class:`MemoryWriteEntry`."""

DEFAULT_MAX_MEMORY_WRITES: Final[int] = 20
"""Security item #7 cap — at most 20 ``memory_writes`` per
:class:`DelegationResult`.  Excess entries are dropped with the
``cap_exceeded`` rejection reason."""

MAX_CONTEXT_PACKAGE_BYTES: Final[int] = 256 * 1024
"""PR #222 deep review S5: hard cap on the JSON-serialised size of
:attr:`DelegationRequest.context_package` and
:attr:`DelegationRequest.output_schema` enforced by the spawner before
dispatch.  Bounds the untrusted-shape input the orchestrator forwards
to a sub-agent (OWASP A05).  256 KiB is a deliberate initial cap; if
operators need to lift it the value should move to ``config/agents.yaml``
in a follow-on rather than be hand-tuned at call sites."""


# ─── Dataclasses ────────────────────────────────────────────────


@dataclass(frozen=True)
class BudgetEnvelope:
    """Resource caps for a sub-agent invocation.

    Mirrors the fields RFC 0008 §E lists on the request envelope.  All
    integer fields are non-negative; a value of ``0`` means "unbounded
    on this axis" (the orchestrator still enforces global caps)."""

    tokens: int = 0
    timeout_seconds: float = 0.0
    max_llm_calls: int = 0

    def __post_init__(self) -> None:
        if self.tokens < 0:
            raise DelegationContractError(
                f"BudgetEnvelope.tokens must be non-negative, got {self.tokens}",
            )
        if self.timeout_seconds < 0:
            raise DelegationContractError(
                f"BudgetEnvelope.timeout_seconds must be non-negative, "
                f"got {self.timeout_seconds}",
            )
        if self.max_llm_calls < 0:
            raise DelegationContractError(
                f"BudgetEnvelope.max_llm_calls must be non-negative, "
                f"got {self.max_llm_calls}",
            )


@dataclass(frozen=True)
class MemoryWriteEntry:
    """A single memory write proposed by a sub-agent.

    The caller's :class:`agents.sub_agents.merge.MergeEngine` validates
    every field, downscales :attr:`importance` to the request's
    ``trust_ceiling``, and rejects entries with caller-set
    :attr:`source_agent` (it is framework-injected on receipt).
    """

    tier: str
    """One of :data:`_ALLOWED_TIERS`.  Procedural tier is rejected with
    the dedicated ``procedural_tier_rejected`` reason so operators can
    spot trust-model probing distinctly from generic ``schema_invalid``."""

    key: str
    content: str
    importance: float = 0.5
    ttl_seconds: float | None = None
    tags: tuple[str, ...] = ()
    merge_strategy: str = "replace"
    source_agent: str | None = None
    """Framework-injected; **must** be ``None`` on the wire.  The merge
    engine sets it from the spawner's record of the originating
    sub-agent ID."""

    def __post_init__(self) -> None:
        # Defer schema validation entirely to MergeEngine so sub-agents
        # can hand back partial / malformed entries and the engine can
        # emit per-entry rejection metrics with structured reasons.
        # Constructor only enforces type-narrow invariants the dataclass
        # itself cannot express.
        if not isinstance(self.tags, tuple):
            object.__setattr__(self, "tags", tuple(self.tags))

    def with_source_agent(self, agent_id: str) -> MemoryWriteEntry:
        """Return a copy with :attr:`source_agent` set to *agent_id*.

        Used by the merge engine in step 2 of the deterministic merge
        order (framework-inject ``source_agent``).  Frozen-dataclass
        ``replace`` keeps the entry immutable."""
        return replace(self, source_agent=agent_id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tags"] = list(self.tags)
        return d


@dataclass(frozen=True)
class DelegationRequest:
    """Caller → sub-agent request envelope (RFC 0008 §E).

    The frozen dataclass is intentionally permissive on construction so
    the spawner can build it from arbitrary caller code; field-level
    validation runs in :meth:`validate` and is invoked by the spawner
    immediately before serialisation.
    """

    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    context_package: dict[str, Any] = field(default_factory=dict)
    budget: BudgetEnvelope = field(default_factory=BudgetEnvelope)
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    output_schema: dict[str, Any] = field(default_factory=dict)
    """JSON-Schema-like description of the expected
    :attr:`DelegationResult.artifacts` shape.

    .. todo::
       PR #222 deep review S1: this field is round-tripped on the wire
       and bounded by :data:`MAX_CONTEXT_PACKAGE_BYTES`, but the merge
       engine does **not** validate :class:`DelegationResult.artifacts`
       against it yet.  Enforcement (jsonschema-based) is a fast-follow
       to PR 3 — callers populating ``output_schema`` today should treat
       it as advisory.  Adding enforcement is mechanical once the
       optional ``jsonschema`` dependency is on the agent runtime.
    """
    trust_ceiling: float = DEFAULT_TRUST_CEILING
    max_memory_writes: int = DEFAULT_MAX_MEMORY_WRITES

    def __post_init__(self) -> None:
        if not isinstance(self.acceptance_criteria, tuple):
            object.__setattr__(
                self, "acceptance_criteria", tuple(self.acceptance_criteria),
            )
        if not isinstance(self.allowed_tools, frozenset):
            object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))

    def validate(self) -> None:
        """Raise :class:`DelegationContractError` on any schema violation.

        Called by the spawner before serialisation so contract violations
        surface in the caller (with caller-side stack), not in the
        sub-agent.
        """
        if not self.objective or not self.objective.strip():
            raise DelegationContractError("DelegationRequest.objective must not be empty")
        if not 0.0 <= self.trust_ceiling <= 1.0:
            raise DelegationContractError(
                f"DelegationRequest.trust_ceiling must be in [0.0, 1.0], "
                f"got {self.trust_ceiling}",
            )
        if self.max_memory_writes < 0:
            raise DelegationContractError(
                f"DelegationRequest.max_memory_writes must be non-negative, "
                f"got {self.max_memory_writes}",
            )
        # BudgetEnvelope validates itself at construction; re-checking would
        # be redundant.

    def to_json(self) -> str:
        payload = {
            "version": 1,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "context_package": self.context_package,
            "budget": {
                "tokens": self.budget.tokens,
                "timeout_seconds": self.budget.timeout_seconds,
                "max_llm_calls": self.budget.max_llm_calls,
            },
            "allowed_tools": sorted(self.allowed_tools),
            "output_schema": self.output_schema,
            "trust_ceiling": self.trust_ceiling,
            "max_memory_writes": self.max_memory_writes,
        }
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_context_value(cls, value: str) -> DelegationRequest:
        """Deserialise a JSON payload produced by :meth:`to_json`."""
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DelegationContractError(
                f"DelegationRequest payload is not valid JSON: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise DelegationContractError(
                "DelegationRequest payload must decode to an object",
            )
        budget_raw = data.get("budget") or {}
        if not isinstance(budget_raw, dict):
            raise DelegationContractError("DelegationRequest.budget must be an object")
        budget = BudgetEnvelope(
            tokens=int(budget_raw.get("tokens", 0)),
            timeout_seconds=float(budget_raw.get("timeout_seconds", 0.0)),
            max_llm_calls=int(budget_raw.get("max_llm_calls", 0)),
        )
        req = cls(
            objective=data.get("objective", ""),
            acceptance_criteria=tuple(data.get("acceptance_criteria") or ()),
            context_package=dict(data.get("context_package") or {}),
            budget=budget,
            allowed_tools=frozenset(data.get("allowed_tools") or ()),
            output_schema=dict(data.get("output_schema") or {}),
            trust_ceiling=float(data.get("trust_ceiling", DEFAULT_TRUST_CEILING)),
            max_memory_writes=int(
                data.get("max_memory_writes", DEFAULT_MAX_MEMORY_WRITES),
            ),
        )
        # PR #222 deep review S4: re-validate on deserialisation.
        # Symmetric with :meth:`DelegationResult.from_metadata_value`,
        # which validates closed-set enums on receipt.  Sub-agent
        # processes are a trust boundary (literal process boundary
        # post-RFC 0009) — a request constructed directly from
        # ``task.context[DELEGATION_REQUEST_KEY]`` must pass the same
        # contract checks the spawner enforces caller-side.
        req.validate()
        return req


@dataclass(frozen=True)
class DelegationResult:
    """Sub-agent → caller result envelope (RFC 0008 §E).

    Fields match the RFC §E table verbatim.  ``status`` is closed-set;
    any other value raises :class:`DelegationContractError` from
    :meth:`validate`.
    """

    summary: str
    status: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    decisions: tuple[str, ...] = ()
    memory_writes: tuple[MemoryWriteEntry, ...] = ()
    risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.decisions, tuple):
            object.__setattr__(self, "decisions", tuple(self.decisions))
        if not isinstance(self.memory_writes, tuple):
            object.__setattr__(self, "memory_writes", tuple(self.memory_writes))
        if not isinstance(self.risks, tuple):
            object.__setattr__(self, "risks", tuple(self.risks))

    def validate(self) -> None:
        """Raise :class:`DelegationContractError` on any schema violation."""
        if self.status not in _ALLOWED_STATUSES:
            raise DelegationContractError(
                f"DelegationResult.status must be one of "
                f"{sorted(_ALLOWED_STATUSES)!r}, got {self.status!r}",
            )
        if not isinstance(self.artifacts, dict):
            raise DelegationContractError(
                "DelegationResult.artifacts must be a dict",
            )

    def to_json(self) -> str:
        payload = {
            "version": 1,
            "summary": self.summary,
            "status": self.status,
            "artifacts": self.artifacts,
            "decisions": list(self.decisions),
            "memory_writes": [e.to_dict() for e in self.memory_writes],
            "risks": list(self.risks),
        }
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_metadata_value(cls, value: str) -> DelegationResult:
        """Deserialise a JSON payload produced by :meth:`to_json`.

        Tolerant of extra/unknown fields (forward-compat) but strict on
        :attr:`status` and on the per-entry ``memory_writes`` shape.
        """
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DelegationContractError(
                f"DelegationResult payload is not valid JSON: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise DelegationContractError(
                "DelegationResult payload must decode to an object",
            )
        writes_raw = data.get("memory_writes") or []
        if not isinstance(writes_raw, list):
            raise DelegationContractError(
                "DelegationResult.memory_writes must be a list",
            )
        entries: list[MemoryWriteEntry] = []
        for raw in writes_raw:
            if not isinstance(raw, dict):
                raise DelegationContractError(
                    "DelegationResult.memory_writes entries must be objects",
                )
            entries.append(
                MemoryWriteEntry(
                    tier=str(raw.get("tier", "")),
                    key=str(raw.get("key", "")),
                    content=str(raw.get("content", "")),
                    importance=float(raw.get("importance", 0.5)),
                    ttl_seconds=(
                        float(raw["ttl_seconds"])
                        if raw.get("ttl_seconds") is not None
                        else None
                    ),
                    tags=tuple(raw.get("tags") or ()),
                    merge_strategy=str(raw.get("merge_strategy", "replace")),
                    source_agent=raw.get("source_agent"),
                ),
            )
        return cls(
            summary=str(data.get("summary", "")),
            status=str(data.get("status", "")),
            artifacts=dict(data.get("artifacts") or {}),
            decisions=tuple(data.get("decisions") or ()),
            memory_writes=tuple(entries),
            risks=tuple(data.get("risks") or ()),
        )


__all__ = [
    "DELEGATION_REQUEST_KEY",
    "DELEGATION_RESULT_KEY",
    "DEFAULT_MAX_MEMORY_WRITES",
    "DEFAULT_TRUST_CEILING",
    "MAX_CONTEXT_PACKAGE_BYTES",
    "BudgetEnvelope",
    "DelegationContractError",
    "DelegationFailure",
    "DelegationRequest",
    "DelegationResult",
    "DelegationStatus",
    "DelegationTier",
    "MemoryWriteEntry",
    "MergeStrategy",
]
