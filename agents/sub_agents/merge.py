"""
Delegation merge engine — applies sub-agent results into the caller's memory.

PR 3 of the [RFC 0008 PR plan](../../docs/rfcs/0008-pr-plan.md).  See
:mod:`agents.sub_agents.delegation` for the request/result contract.

Merge order (deterministic, RFC 0008 §E)::

    1. schema validation (whole result)
    2. cap ``memory_writes`` at ``max_memory_writes``           → cap_exceeded
    3. per-entry pipeline:
         a. validate entry schema (tier / key / content / etc.) → schema_invalid /
            procedural_tier_rejected / source_agent_set / reserved_tag_prefix
         b. framework-inject ``source_agent``
         c. downscale ``importance`` to ``trust_ceiling``       → trust_ceiling
         d. apply per-entry merge strategy against existing memory
    4. emit metrics

Why cap is run before per-entry validation: cap_exceeded entries are not
double-counted under schema_invalid (and per-entry validation is not
wasted work on entries that the cap will discard anyway).  See the
in-code comment on the cap step.

Failure at step 1 raises :class:`DelegationFailure` (no partial merge).
The per-entry steps reject only the offending entries; the surviving
entries continue through the merge.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .delegation import (
    _ALLOWED_STRATEGIES,
    _ALLOWED_TIERS,
    DelegationContractError,
    DelegationFailure,
    DelegationRequest,
    DelegationResult,
    MemoryWriteEntry,
)

logger = logging.getLogger(__name__)


# ─── Rejection-reason taxonomy (mirrors observability metric labels) ─

REASON_SCHEMA_INVALID = "schema_invalid"
REASON_TRUST_CEILING = "trust_ceiling"
"""Reason label for the ``delegation_memory_writes_downscaled`` metric.

See PR #222 deep review S2: the per-entry trust-ceiling event is a
*modifier* on an admitted entry, not a separate admission bucket.  It
is emitted under its own metric so operators summing
``delegation_memory_writes_admitted`` across ``reason`` labels do not
double-count the admission."""
REASON_CAP_EXCEEDED = "cap_exceeded"
REASON_SOURCE_AGENT_SET = "source_agent_set"
REASON_PROCEDURAL_TIER_REJECTED = "procedural_tier_rejected"
REASON_RESERVED_TAG_PREFIX = "reserved_tag_prefix"
"""Reason label for sub-agent-supplied tags whose prefix collides with
framework-prefixed provenance carriers (``tier:``, ``key:``, ``source:``,
``channel:``).  See PR #222 deep review S3 — same trust-boundary
semantics as :data:`REASON_SOURCE_AGENT_SET`, just for tags rather than
the ``source_agent`` field."""
REASON_CONFLICT = "conflict"

# Tag prefixes the framework reserves for its own provenance carriers.
# Sub-agents that emit tags with these prefixes are rejected so they
# cannot spoof tier / key / source / channel metadata that downstream
# tag-prefix consumers (e.g. RFC 0011 channel-scoped recall) trust.
RESERVED_TAG_PREFIXES: frozenset[str] = frozenset({
    "tier:", "key:", "source:", "channel:",
})


@dataclass(frozen=True)
class RejectedEntry:
    """A single rejected :class:`MemoryWriteEntry` with reason + offset.

    The offset preserves caller-side input order so operators can map a
    rejection back to the sub-agent's emitted result without relying on
    object identity (which is lost across the JSON wire).
    """

    index: int
    reason: str
    detail: str = ""


@dataclass
class MergeOutcome:
    """Result of :meth:`MergeEngine.merge_result`.

    ``admitted`` and ``rejected`` together account for every incoming
    :class:`MemoryWriteEntry` exactly once."""

    admitted: list[MemoryWriteEntry] = field(default_factory=list)
    rejected: list[RejectedEntry] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    """Caller-merged artifacts (post-strategy application)."""

    def reasons(self) -> dict[str, int]:
        """Aggregate ``{reason: count}`` for metric emission."""
        out: dict[str, int] = {}
        for r in self.rejected:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out


# ─── JSON Merge Patch (RFC 7396) ─────────────────────────────────


def apply_json_merge_patch(target: Any, patch: Any) -> Any:
    """Apply RFC 7396 JSON Merge Patch.

    Per [RFC 0008 PR 3 plan](../../docs/rfcs/0008-pr-plan.md):

    * objects merge recursively; ``null`` values **delete** the key
    * non-object patches replace ``target`` wholesale
    * arrays / scalars at the patch root replace ``target``
    """
    if not isinstance(patch, Mapping):
        return patch
    if not isinstance(target, Mapping):
        target = {}
    out: dict[str, Any] = dict(target)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = apply_json_merge_patch(out.get(key), value)
    return out


def _union_tags(existing: list[str], incoming: list[str]) -> list[str]:
    """Order-preserving tag union — existing first, then new entries."""
    seen = set(existing)
    out = list(existing)
    for tag in incoming:
        if tag not in seen:
            out.append(tag)
            seen.add(tag)
    return out


# ─── MergeEngine ────────────────────────────────────────────────


class MergeEngine:
    """Apply :class:`DelegationResult` envelopes to caller-side state.

    The engine is deliberately stateless apart from the optional
    *existing_keys* set passed to :meth:`merge_result` (used for
    ``reject_on_conflict`` detection without a by-key facade lookup, which
    PR 3 does not introduce — the by-key lookup arrives in PR 5 with the
    procedural tier).

    Parameters
    ----------
    on_metric:
        Optional callable invoked as ``on_metric(name, labels, value=1)``
        for each rejected entry and admitted entry.  When ``None`` (the
        default), metrics are emitted as structured log records under
        ``agents.sub_agents.merge`` — the Go observability metrics back-fill
        ships in the follow-on ``feature/v030-rfc0008-delegation-metrics``
        PR per the PR 3 sizing-risk note.
    """

    def __init__(self, on_metric: Any = None) -> None:
        self._on_metric = on_metric

    # -- public API -------------------------------------------------

    def merge_result(
        self,
        result: DelegationResult,
        request: DelegationRequest,
        *,
        source_agent: str,
        existing_artifacts: Mapping[str, Any] | None = None,
        existing_keys: Iterable[str] = (),
    ) -> MergeOutcome:
        """Run the deterministic 6-step merge pipeline.

        Parameters
        ----------
        result:
            The sub-agent's :class:`DelegationResult` (already
            deserialised from :data:`DELEGATION_RESULT_KEY`).
        request:
            The originating :class:`DelegationRequest` — supplies
            ``trust_ceiling`` and ``max_memory_writes``.
        source_agent:
            Spawner-recorded ID of the sub-agent that produced *result*.
            Framework-injected onto every admitted entry (step 2).
        existing_artifacts:
            Optional caller-side artifact dict to merge into.  When
            ``None`` (default) :attr:`MergeOutcome.artifacts` reflects
            the result's artifacts only.  Per-key strategies on
            artifacts are not encoded in :class:`DelegationResult` —
            artifacts merge via straightforward JSON Merge Patch when
            supplied (the per-entry ``merge_strategy`` field governs
            ``memory_writes``, not artifacts).
        existing_keys:
            Set of caller-known memory keys used for
            ``reject_on_conflict`` detection on memory_writes.
        """
        # Step 1: whole-result schema validation.
        try:
            result.validate()
        except DelegationContractError as exc:
            self._emit("delegation_merge_outcome", {"status": "failed"})
            raise DelegationFailure(
                f"DelegationResult schema invalid: {exc}",
            ) from exc

        outcome = MergeOutcome()
        outcome.artifacts = apply_json_merge_patch(
            dict(existing_artifacts) if existing_artifacts else {},
            result.artifacts,
        )

        # Step 3: cap (run before per-entry validation so cap_exceeded
        # entries are not double-counted under schema_invalid).
        cap = request.max_memory_writes
        capped: list[tuple[int, MemoryWriteEntry]] = []
        for idx, entry in enumerate(result.memory_writes):
            if idx >= cap:
                outcome.rejected.append(
                    RejectedEntry(
                        index=idx,
                        reason=REASON_CAP_EXCEEDED,
                        detail=f"max_memory_writes={cap}",
                    ),
                )
                continue
            capped.append((idx, entry))

        # Steps 2, 4, 5 — per-entry pipeline.
        known_keys = set(existing_keys)
        for idx, entry in capped:
            rejection = self._validate_entry(idx, entry)
            if rejection is not None:
                outcome.rejected.append(rejection)
                continue

            # Step 2: framework-inject source_agent.
            entry = entry.with_source_agent(source_agent)

            # Step 4: trust-ceiling downscale.
            #
            # PR #222 deep review S2: emit a *separate* metric
            # (``delegation_memory_writes_downscaled``) rather than
            # re-using the admitted metric with a different ``reason``
            # label — the entry is still admitted (counted once at
            # step 6 under reason=ok), the downscale is a modifier on
            # that admission, not a parallel admission bucket.
            #
            # PR #222 deep review N1: rebuild via ``dataclasses.replace``
            # so future fields on :class:`MemoryWriteEntry` are not
            # silently dropped on downscale.
            if entry.importance > request.trust_ceiling:
                self._emit(
                    "delegation_memory_writes_downscaled",
                    {"reason": REASON_TRUST_CEILING},
                )
                entry = replace(entry, importance=request.trust_ceiling)

            # Step 5: per-entry merge strategy against existing memory.
            applied = self._apply_strategy(idx, entry, known_keys)
            if isinstance(applied, RejectedEntry):
                outcome.rejected.append(applied)
                continue

            outcome.admitted.append(applied)
            known_keys.add(applied.key)

        # Step 6: metrics.
        self._emit(
            "delegation_merge_outcome",
            {"status": result.status},
        )
        for reason, count in outcome.reasons().items():
            self._emit(
                "delegation_memory_writes_rejected",
                {"reason": reason},
                value=count,
            )
        if outcome.admitted:
            self._emit(
                "delegation_memory_writes_admitted",
                {"reason": "ok"},
                value=len(outcome.admitted),
            )

        return outcome

    def merge_artifacts(
        self,
        existing: Any,
        incoming: Any,
        strategy: str,
    ) -> Any:
        """Apply *strategy* to merge *incoming* into *existing*.

        Used by callers that accumulate artifacts across multiple
        delegation rounds (e.g. a workflow step that fans out to several
        sub-agents and aggregates).  Raises
        :class:`DelegationContractError` for schema-incompatible
        strategy/value combinations (e.g. ``append`` on non-list)."""
        if strategy not in _ALLOWED_STRATEGIES:
            raise DelegationContractError(
                f"merge strategy must be one of {sorted(_ALLOWED_STRATEGIES)!r}, "
                f"got {strategy!r}",
            )
        if strategy == "replace":
            return incoming
        if strategy == "append":
            if not isinstance(existing, list) or not isinstance(incoming, list):
                raise DelegationContractError(
                    "append strategy requires list-typed existing and incoming "
                    "values",
                )
            return [*existing, *incoming]
        if strategy == "patch":
            # Per plan: tag-list union is a top-level concern; here we
            # implement the structured-object case via JSON Merge Patch
            # and fall back to replace-for-strings.
            if isinstance(existing, list) and isinstance(incoming, list):
                # tag-list semantics: union, order-preserving.
                return _union_tags([str(x) for x in existing], [str(x) for x in incoming])
            if not isinstance(existing, Mapping) or not isinstance(incoming, Mapping):
                # replace-for-strings (and other scalars).
                return incoming
            return apply_json_merge_patch(existing, incoming)
        # reject_on_conflict
        if existing is None:
            return incoming
        raise DelegationContractError(
            "reject_on_conflict: existing value present, refusing to overwrite",
        )

    # -- internals --------------------------------------------------

    def _validate_entry(
        self, idx: int, entry: MemoryWriteEntry,
    ) -> RejectedEntry | None:
        """Per-entry schema check.  Returns ``None`` when the entry passes."""
        if entry.source_agent is not None:
            return RejectedEntry(
                index=idx,
                reason=REASON_SOURCE_AGENT_SET,
                detail=f"caller-set source_agent={entry.source_agent!r}",
            )
        # PR #222 deep review S3: reject sub-agent-supplied tags whose
        # prefix collides with framework provenance carriers.  Without
        # this check a sub-agent can emit ``tags=("source:legitimate",)``
        # that, after _persist_admitted concatenates the framework
        # ``source:<agent_id>`` tag, leaves the stored entry with two
        # ``source:*`` tags \u2014 spoofing provenance to any downstream
        # consumer that parses tag prefixes (RFC 0011 channel-scoped
        # recall, ACL filters, etc.).  Same trust-boundary rationale as
        # the existing ``source_agent`` field check immediately above.
        for tag in entry.tags:
            for prefix in RESERVED_TAG_PREFIXES:
                if tag.startswith(prefix):
                    return RejectedEntry(
                        index=idx,
                        reason=REASON_RESERVED_TAG_PREFIX,
                        detail=(
                            f"tag {tag!r} uses reserved prefix {prefix!r} "
                            f"(reserved: {sorted(RESERVED_TAG_PREFIXES)!r})"
                        ),
                    )
        if entry.tier == "procedural":
            # Distinct reason so operators can spot trust-model probing.
            return RejectedEntry(
                index=idx,
                reason=REASON_PROCEDURAL_TIER_REJECTED,
                detail="procedural tier is not delegatable",
            )
        if entry.tier not in _ALLOWED_TIERS:
            return RejectedEntry(
                index=idx,
                reason=REASON_SCHEMA_INVALID,
                detail=f"tier must be one of {sorted(_ALLOWED_TIERS)!r}",
            )
        if not entry.key or not entry.key.strip():
            return RejectedEntry(
                index=idx,
                reason=REASON_SCHEMA_INVALID,
                detail="key must not be empty",
            )
        if not entry.content or not entry.content.strip():
            return RejectedEntry(
                index=idx,
                reason=REASON_SCHEMA_INVALID,
                detail="content must not be empty",
            )
        if not 0.0 <= entry.importance <= 1.0:
            return RejectedEntry(
                index=idx,
                reason=REASON_SCHEMA_INVALID,
                detail=f"importance must be in [0.0, 1.0], got {entry.importance}",
            )
        if entry.ttl_seconds is not None and entry.ttl_seconds <= 0:
            return RejectedEntry(
                index=idx,
                reason=REASON_SCHEMA_INVALID,
                detail=f"ttl_seconds must be positive, got {entry.ttl_seconds}",
            )
        if entry.merge_strategy not in _ALLOWED_STRATEGIES:
            return RejectedEntry(
                index=idx,
                reason=REASON_SCHEMA_INVALID,
                detail=(
                    f"merge_strategy must be one of "
                    f"{sorted(_ALLOWED_STRATEGIES)!r}, got {entry.merge_strategy!r}"
                ),
            )
        return None

    def _apply_strategy(
        self,
        idx: int,
        entry: MemoryWriteEntry,
        known_keys: set[str],
    ) -> MemoryWriteEntry | RejectedEntry:
        """Step 5: per-entry strategy against in-merge known-key set.

        ``replace`` / ``append`` always admit; ``patch`` admits unchanged
        (the actual structural merge occurs at the artifact level —
        memory entries themselves are append-only at the storage tier in
        Phase 2).  ``reject_on_conflict`` admits only when *known_keys*
        does not contain :attr:`MemoryWriteEntry.key`.
        """
        if entry.merge_strategy == "reject_on_conflict" and entry.key in known_keys:
            return RejectedEntry(
                index=idx,
                reason=REASON_CONFLICT,
                detail=f"key={entry.key!r} already present",
            )
        return entry

    def _emit(
        self,
        metric: str,
        labels: dict[str, str],
        *,
        value: int = 1,
    ) -> None:
        if self._on_metric is not None:
            try:
                self._on_metric(metric, labels, value)
            except Exception:  # pragma: no cover — metrics must not raise
                logger.exception("delegation metric callback failed: %s", metric)
            return
        # Structured log — back-fill to Go counters lands in the
        # delegation-metrics follow-on PR (sizing-risk split).
        logger.info(
            "delegation_metric",
            extra={"metric": metric, "labels": labels, "value": value},
        )


__all__ = [
    "REASON_CAP_EXCEEDED",
    "REASON_CONFLICT",
    "REASON_PROCEDURAL_TIER_REJECTED",
    "REASON_RESERVED_TAG_PREFIX",
    "REASON_SCHEMA_INVALID",
    "REASON_SOURCE_AGENT_SET",
    "REASON_TRUST_CEILING",
    "RESERVED_TAG_PREFIXES",
    "MergeEngine",
    "MergeOutcome",
    "RejectedEntry",
    "apply_json_merge_patch",
]
