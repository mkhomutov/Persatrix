"""Shared channel-payload contract (RFC 0040 Phase 1).

The agent↔orchestrator channel-publish/history payload shapes are pinned
in ``schemas/channel.schema.json`` under three definitions:

* ``publishMessageRequest`` — the POST body this side assembles,
* ``channelMessage`` — one message on a history/publish response,
* ``channelHistoryResponse`` — the history envelope.

This module is the one place that loads them, so the send side
(:mod:`agents.channel_publisher`), any future read-side consumer, and
the contract tests all validate against the same document rather than
three drifting copies — which is the drift risk
[RFC 0040](docs/rfcs/0040-agent-orchestrator-transport-unification.md)
Motivation 1 names, closed here *before* the Phase 2 dual-surface window
opens rather than during it.

**Fail-open.** :func:`validate_publish_payload` never raises — not on a
contract violation, and not on a schema this module cannot use (a
missing file, an unresolvable ``$ref``, a malformed definition). Both
log WARN and return. RFC 0040's migration
guarantee is "no flag day": Phase 1 is additive hardening over a REST
path that already works, so a schema bug must not be able to take down
publishing in a deployment that was healthy a release earlier. The
enforcement teeth are at test time (``make test`` runs the contract and
cross-language drift pins), where a violation is a red build rather than
a dropped message. This is the plan's documented default; PR review may
flip it to fail-closed once the schema has a release of field exposure.

**Schema availability.** The schema is read from the repo/image root at
first use and cached. If it is missing or unparseable the validator
degrades to a no-op after a single WARN (never one per publish) — the
publish path stays functional on an image that shipped without
``schemas/``.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

__all__ = [
    "CHANNEL_SCHEMA_PATH",
    "channel_payload_schema",
    "subschema",
    "validate_publish_payload",
]

logger = logging.getLogger(__name__)

# Repo-root-relative: ``agents/channel_payload_contract.py`` → ``<root>``.
# Resolved from ``__file__`` rather than the process CWD because the agent
# runs from ``/app`` in the container and from arbitrary directories under
# pytest; a CWD-relative path would silently miss in one of the two.
# Dockerfile.agent copies ``schemas/`` for exactly this lookup.
CHANNEL_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "channel.schema.json"
)

# Definition names in ``schemas/channel.schema.json``. Named here so a
# rename on either side fails the contract test rather than degrading to
# a permanently-skipped validation.
_PUBLISH_REQUEST_DEF = "publishMessageRequest"

# Latched by :func:`_disable_after_fault` when the validator itself
# raises (unusable schema), after which validation is a no-op for the
# rest of the process. Module-level rather than a cache entry because it
# must survive across definitions: a document broken enough to fault one
# walk is not to be trusted for another.
_faulted = False


@lru_cache(maxsize=1)
def channel_payload_schema() -> dict[str, Any] | None:
    """Load and cache ``schemas/channel.schema.json``.

    Returns ``None`` (after one WARN) when the file is absent or not
    valid JSON, so callers degrade to a no-op instead of raising on a
    deployment whose image omitted the schema.
    """
    try:
        return json.loads(CHANNEL_SCHEMA_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, ValueError) as exc:
        # lru_cache means this WARN fires at most once per process, not
        # once per publish — a missing schema is a deployment fact, not a
        # per-message event, and logging it per publish would bury the
        # signal it is meant to carry.
        logger.warning(
            "channels: payload contract unavailable (%s: %s); "
            "publish-payload validation disabled for this process",
            CHANNEL_SCHEMA_PATH, exc,
        )
        return None


def subschema(name: str) -> dict[str, Any] | None:
    """Return definition ``name`` as a standalone, ``$ref``-resolvable schema.

    The definitions cross-reference each other (``channelHistoryResponse``
    → ``channelMessage`` → ``messageMetadata``) with ``#/definitions/...``
    pointers, which resolve against the *document root*. Lifting a bare
    definition out would leave those pointers dangling, so the returned
    schema carries the full ``definitions`` map as its own root alongside
    the definition's own keywords.
    """
    schema = channel_payload_schema()
    if schema is None:
        return None
    definitions = schema.get("definitions", {})
    if name not in definitions:
        logger.warning(
            "channels: payload contract has no definition %r; "
            "validation against it disabled", name,
        )
        return None
    return {
        **definitions[name],
        "definitions": definitions,
        "$schema": schema.get("$schema", "http://json-schema.org/draft-07/schema#"),
    }


@lru_cache(maxsize=8)
def _validator(name: str) -> Any | None:
    """Compiled, cached validator for definition ``name``.

    Compiling once per process keeps the per-publish cost to the
    instance walk; re-compiling per call would put schema parsing on the
    publish hot path.

    Returns ``None`` on a malformed definition (a non-object entry the
    ``**`` lift chokes on, a keyword ``Draft7Validator`` construction
    rejects) for the same reason a missing file does: the caller degrades
    to a no-op rather than raising on the publish path.
    """
    try:
        sub = subschema(name)
        if sub is None:
            return None
        return jsonschema.Draft7Validator(sub)
    except Exception as exc:  # noqa: BLE001 — fail-open, see module docstring
        _disable_after_fault(f"compiling definition {name!r}", exc)
        return None


def _disable_after_fault(during: str, exc: BaseException) -> None:
    """Latch validation off for the process after a validator-side fault.

    A fault here is a *schema* bug, not a payload bug, so it repeats on
    every publish: without the latch the same WARN would print per
    message and the failed walk would be re-paid per message. Same
    degrade shape as an absent schema file — one WARN, then a no-op —
    and the same reason (:mod:`~agents.channel_payload_contract` module
    docstring): a contract bug must not be able to take down publishing
    on a deployment that was healthy a release earlier.
    """
    global _faulted
    if _faulted:
        return
    _faulted = True
    logger.warning(
        "channels: publish-payload validation failed while %s (%s: %s); "
        "validation disabled for this process, publishing continues "
        "(fail-open). This is a contract bug in %s, not a payload bug — "
        "the contract tests are where it should have surfaced.",
        during, type(exc).__name__, exc, CHANNEL_SCHEMA_PATH,
    )


def validate_publish_payload(payload: dict[str, Any]) -> list[str]:
    """Validate an assembled publish body; log and return any violations.

    Fail-open by contract: the return value is a list of human-readable
    messages (empty when clean) and is *advisory*. Callers publish
    regardless — see the module docstring for why. The list is returned
    rather than only logged so tests can assert on it directly without
    scraping log records.

    "Fail-open" covers *validator* faults as well as payload violations.
    ``iter_errors`` raises — it does not merely report — when the schema
    itself is unusable: an unresolvable ``$ref`` (say ``messageMetadata``
    moved to its own file while ``publishMessageRequest.metadata`` still
    points at ``#/definitions/messageMetadata``) surfaces as a
    ``referencing`` error mid-walk, and only on payloads that reach the
    broken subschema. Uncaught, that escapes into
    :meth:`HTTPChannelPublisher.publish`, whose ``except Exception``
    re-raises it, and the executor records ``status="failed"`` — a
    dropped message per publish carrying ``metadata``, i.e. every
    same-channel reply (``DispatchContext.same_channel_claim`` stamps
    ``interaction_id`` on all of them). A schema-shape mistake must not
    become a publish outage, so the walk is guarded and the process
    latches to a no-op.
    """
    if _faulted:
        return []
    validator = _validator(_PUBLISH_REQUEST_DEF)
    if validator is None:
        return []
    try:
        errors = [
            # ``json_path`` gives ``$.metadata.cascade_depth`` rather than a
            # deque, so the WARN names the offending field directly.
            f"{err.json_path}: {err.message}"
            for err in sorted(validator.iter_errors(payload), key=lambda e: e.json_path)
        ]
    except Exception as exc:  # noqa: BLE001 — fail-open, see the docstring
        _disable_after_fault("validating a publish payload", exc)
        return []
    if errors:
        logger.warning(
            "channels: publish payload violates the RFC 0040 Phase 1 contract "
            "(%s); publishing anyway (fail-open)",
            "; ".join(errors),
        )
    return errors
