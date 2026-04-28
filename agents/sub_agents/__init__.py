"""Persatrix Sub-Agent System (RFC 0008 PR 3+)."""

from .delegation import (
    DEFAULT_MAX_MEMORY_WRITES,
    DEFAULT_TRUST_CEILING,
    DELEGATION_REQUEST_KEY,
    DELEGATION_RESULT_KEY,
    BudgetEnvelope,
    DelegationContractError,
    DelegationFailure,
    DelegationRequest,
    DelegationResult,
    MemoryWriteEntry,
)
from .merge import MergeEngine, MergeOutcome, RejectedEntry, apply_json_merge_patch
from .spawner import FacadeBoundSpawner, SpawnResult, SubAgentSpawner

__all__ = [
    "DEFAULT_MAX_MEMORY_WRITES",
    "DEFAULT_TRUST_CEILING",
    "DELEGATION_REQUEST_KEY",
    "DELEGATION_RESULT_KEY",
    "BudgetEnvelope",
    "DelegationContractError",
    "DelegationFailure",
    "DelegationRequest",
    "DelegationResult",
    "FacadeBoundSpawner",
    "MemoryWriteEntry",
    "MergeEngine",
    "MergeOutcome",
    "RejectedEntry",
    "SpawnResult",
    "SubAgentSpawner",
    "apply_json_merge_patch",
]
