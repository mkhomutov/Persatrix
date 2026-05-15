"""Declarative-fact extractor wired at interaction close (RFC 0026 PR 2).

Pulls the JSON list of fact tuples out of the combined summarize +
extract LLM response, applies the predicate allowlist + subject
canonicalization, and dispatches :meth:`FactStore.store` per tuple.

The module is deliberately I/O-free aside from the
:func:`store_extracted_facts` writes — prompt construction, response
splitting, and parsing are pure functions so the RFC 0020 PR 4
close-path orchestrator can mock the LLM half and test the extractor
half in isolation.

Atomicity contract (RFC 0026 §Phase 1 step 4)
---------------------------------------------
The summarize + extract round-trip is one LLM call.  When the model
returns a well-formed envelope ``{"summary": "...", "facts": [...]}``,
both halves commit.  When the ``facts`` half is malformed JSON or has
the wrong shape, the caller commits the summary half and increments
``agent.facts.extraction_failed`` — surfacing the failure to operators
without losing the prose summary.  Per-tuple failures (allowlist miss,
missing required field) are caught inside
:func:`store_extracted_facts` so one bad tuple does not drop the rest
of the batch.

Prompt
------
The combined prompt asks the model to emit a single JSON object with
two keys, ``summary`` (string) and ``facts`` (list of tuple dicts).
:func:`build_combined_prompt_suffix` returns the addition appended to
the existing summary prompt — keeping the summary prompt body
unchanged keeps the RFC 0020 PR 4 regression suite intact.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from ..memory.fact_predicates import (
    PREDICATE_ALLOWLIST,
    canonicalize_subject,
    validate_predicate,
)
from ..memory.facts import FactStore
from ..observability.metrics import current_agent_id, try_get_instruments
from ..prompt_loader import load_snippet

# Parser surface lives in ``fact_envelope`` so the storage-dispatch
# module stays under the 500-line review-friendly cap; re-exported
# here so existing imports (``from agents.persona_runtime.fact_extractor
# import FactsParseError, split_combined_response, parse_facts_payload``)
# continue to resolve without any caller edit.
from .fact_envelope import (
    FactsParseError,
    parse_facts_payload,
    split_combined_response,
)

if TYPE_CHECKING:
    from ..memory.interactions import Interaction

logger = logging.getLogger(__name__)

__all__ = [
    "FactsParseError",
    "build_combined_prompt_suffix",
    "dispatch_facts_from_response",
    "parse_facts_payload",
    "split_combined_response",
    "store_extracted_facts",
]


# ─── Rejected-predicate discovery telemetry ─────────────────
#
# RFC 0026 PR 2 review decision — the predicate allowlist is the
# storage-boundary cap on prompt-injection blast radius, but it is also
# the bound on what the LLM can record.  An adversarial verb is a
# security signal; a recurring near-miss verb ("has_kid_named" when the
# allowlist has "has_child_named") is a quality signal that the
# vocabulary needs an amendment.  Both share one path: log the rejected
# verb once per (process, predicate) pair so operators can mine the
# discovery surface without log-volume blowup.
#
# Dedup is per-verb, capped at _REJECTED_PREDICATES_LOG_CAP distinct
# strings so a pathological LLM emitting unique-per-rejection garbage
# cannot grow the in-process set without bound.  After the cap, further
# distinct verbs are silently dropped — the goal is operator discovery
# of recurring patterns, not exhaustive capture.
_REJECTED_PREDICATES_SEEN: set[str] = set()
_REJECTED_PREDICATES_LOG_CAP = 256


def _record_rejected_predicate(predicate: str) -> None:
    """Log a rejected predicate verbatim on its first occurrence per process.

    Emits one WARNING-level record with the verb in a structured
    ``persatrix.facts.rejected_predicate`` extra field — separate from
    the per-tuple WARNING that logs the full raw dict (object value
    included).  The structured field is the aggregation-friendly
    surface for log pipelines; the per-tuple log keeps the debugging
    context.
    """
    if not predicate:
        return
    if predicate in _REJECTED_PREDICATES_SEEN:
        return
    if len(_REJECTED_PREDICATES_SEEN) >= _REJECTED_PREDICATES_LOG_CAP:
        return
    _REJECTED_PREDICATES_SEEN.add(predicate)
    logger.warning(
        "rfc0026.predicate_rejected predicate=%r", predicate,
        extra={"persatrix.facts.rejected_predicate": predicate},
    )


def _reset_rejected_predicates_seen() -> None:
    """Test-only — drop the per-process dedup set.

    Production code never calls this; the dedup set is a process-scoped
    discovery aid that lives until process exit.  Tests that exercise
    the rejection path call this in setup so the dedup state from a
    prior test does not mask a fresh emission.
    """
    _REJECTED_PREDICATES_SEEN.clear()


# ─── Prompt construction ────────────────────────────────────


def build_combined_prompt_suffix() -> str:
    """Return the addition that turns the summary prompt into a
    combined summarize + extract prompt.

    The summary prompt body lives in
    ``prompts/runtime/safety/interaction-summarizer.md`` (kept
    unchanged so the RFC 0020 PR 4 regression suite stays green).
    This suffix asks the model to emit one JSON object with two
    keys — the same envelope :func:`split_combined_response` parses.

    The prompt body itself lives in
    ``prompts/runtime/safety/fact-extractor-suffix.md`` and is
    loaded via :func:`load_snippet` (same deny-by-default rules
    every other runtime prompt asset uses).  The ``{predicate_list}``
    placeholder is substituted with the sorted vocabulary so the
    model can output valid tuples on the first round-trip —
    authors of new predicates add the verb to
    :data:`PREDICATE_ALLOWLIST` and the prompt re-renders
    automatically.  The leading blank-line separator and trailing
    newline are added here so the snippet's bytes-on-disk match
    what the LLM ultimately sees.
    """
    predicate_list = ", ".join(sorted(PREDICATE_ALLOWLIST))
    body = load_snippet("fact-extractor-suffix").format(
        predicate_list=predicate_list,
    )
    return "\n\n" + body + "\n"


# ─── Storage dispatch ───────────────────────────────────────


async def store_extracted_facts(
    fact_store: FactStore,
    *,
    facts: Iterable[Mapping[str, Any]],
    source_interaction_id: str,
    asserted_at: float,
    session_id: str,
    sender_id: str | None = None,
) -> int:
    """Persist each parsed fact via :meth:`FactStore.store`.

    Returns the count of successfully-stored rows.  Per-tuple failures
    (missing required field, allowlist miss, certainty out of range,
    subject canonicalization rejection) are caught here and increment
    ``agent.facts.extraction_failed`` — one bad tuple does not drop
    the rest of the batch.

    Subject canonicalization
    ------------------------
    Each tuple's ``subject`` is normalised via
    :func:`agents.memory.fact_predicates.canonicalize_subject`.  When
    ``sender_id`` is supplied and the canonicalised subject matches
    ``canonicalize_subject(sender_id)``, the row is stored under that
    canonical sender form — so a fact about the counterparty joins
    the relationship row that already keys on the canonical sender.
    RFC 0026 §C.

    PR #340 review S1: the substitution branch returns the
    **canonical** form of ``sender_id`` (see :func:`_canonicalize_subject`).

    PR #340 deep-review S3: ``sender_id`` is stripped at the boundary
    so whitespace collapses to ``None`` before reaching
    :func:`canonicalize_subject` (which would otherwise raise *before*
    the per-tuple try-block and drop the whole batch silently).

    Predicate canonicalization
    --------------------------
    Predicates are downcased + stripped before reaching the
    allowlist.  Capitalised variants from the LLM normalise to the
    canonical form rather than counting as a rejection.
    """
    if sender_id is not None:
        sender_id = sender_id.strip() or None
    canonical_sender = (
        canonicalize_subject(sender_id) if sender_id else None
    )
    stored = 0
    failures = 0
    for raw_fact in facts:
        try:
            subject = _canonicalize_subject(
                raw_fact, sender_id=sender_id,
                canonical_sender=canonical_sender,
            )
            predicate = _normalise_predicate(raw_fact)
            object_ = _required_str(raw_fact, "object")
            certainty = _coerce_certainty(raw_fact)
        except (ValueError, KeyError, TypeError) as exc:
            failures += 1
            logger.warning(
                "Fact tuple rejected (raw=%r): %s", raw_fact, exc,
            )
            continue

        try:
            await fact_store.store(
                subject=subject,
                predicate=predicate,
                object=object_,
                certainty=certainty,
                source_interaction_id=source_interaction_id,
                asserted_at=asserted_at,
                session_id=session_id,
            )
        except ValueError as exc:
            failures += 1
            logger.warning(
                "FactStore.store rejected tuple (raw=%r): %s",
                raw_fact, exc,
            )
            continue
        stored += 1

    if failures:
        _emit_extraction_failed(failures)
    return stored


# ─── Internal helpers ───────────────────────────────────────


def _required_str(raw: Mapping[str, Any], key: str) -> str:
    if key not in raw:
        raise KeyError(f"fact tuple missing required key {key!r}")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"fact tuple field {key!r} must be a non-empty string, "
            f"got {value!r}",
        )
    return value


def _canonicalize_subject(
    raw: Mapping[str, Any],
    *,
    sender_id: str | None,
    canonical_sender: str | None,
) -> str:
    raw_subject = _required_str(raw, "subject")
    canonical = canonicalize_subject(raw_subject)
    # RFC 0026 §C counterparty mapping: when the LLM names the
    # counterparty by display name and it matches the channel
    # sender_id (after canonicalisation), substitute the **canonical**
    # sender_id so the row joins the relationship row on the same key.
    # PR #340 review S1: returning raw ``sender_id`` here would split
    # rows for mixed-case sender_ids (e.g. ``"Bob_user_123"``) — the
    # substitution branch must yield the same canonical key any other
    # write path would produce.
    if (
        sender_id is not None
        and canonical_sender is not None
        and canonical == canonical_sender
    ):
        return canonical_sender
    return canonical


def _normalise_predicate(raw: Mapping[str, Any]) -> str:
    predicate = _required_str(raw, "predicate").strip()
    # The validator is case-sensitive (allowlist verbs are lowercase);
    # downcase before validating so a stray ``"Has_Name"`` from the
    # LLM normalises rather than rejecting.  ``self.*`` predicates
    # already follow the dotted-lowercase convention.
    predicate = predicate.lower()
    try:
        validate_predicate(predicate)
    except ValueError:
        # The LLM emitted a verb the allowlist does not carry.  Record
        # the verbatim string (post-normalisation) so operators can
        # mine the discovery surface — see
        # :func:`_record_rejected_predicate`.  Missing / empty
        # predicate fields are rejected by :func:`_required_str`
        # before reaching this branch, so the recording fires only
        # for genuine vocabulary misses.
        _record_rejected_predicate(predicate)
        raise
    return predicate


def _coerce_certainty(raw: Mapping[str, Any]) -> float:
    if "certainty" not in raw:
        return 1.0
    value = raw["certainty"]
    if isinstance(value, bool):  # bool is a subclass of int — reject explicitly
        raise ValueError("certainty must be a number, not bool")
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"certainty must be a number, got {type(value).__name__}",
        )
    coerced = float(value)
    if not 0.0 <= coerced <= 1.0:
        raise ValueError(
            f"certainty must be in [0.0, 1.0], got {coerced}",
        )
    return coerced


def _emit_extraction_failed(count: int) -> None:
    """Increment ``agent.facts.extraction_failed`` by ``count``.

    Metrics-backend failure must not surface as a write failure —
    mirrors the suppression pattern used in
    :meth:`FactStore.store` for ``facts_stored`` / ``facts_superseded``.
    """
    with contextlib.suppress(Exception):
        inst = try_get_instruments()
        if inst is not None:
            inst.facts_extraction_failed.add(
                count, attributes={"agent.id": current_agent_id()},
            )


# ─── Close-path orchestration ──────────────────────────────


async def dispatch_facts_from_response(
    *,
    fact_store: FactStore,
    facts_raw: str,
    interaction: Interaction,
    agent_id: str,
    session_id: str,
) -> None:
    """Parse + persist the facts half of the combined LLM response.

    Wired by :func:`agents.persona_runtime.summarize_close.finalize_closed_interaction`
    after the summary commits.  Catches malformed ``facts`` JSON and
    increments ``agent.facts.extraction_failed`` once — RFC 0026
    §Phase 1 step 4 rollback policy.  Per-tuple failures
    (allowlist miss, missing required field) are caught by
    :func:`store_extracted_facts` and counted there.

    ``interaction.interaction_id`` must be set; the caller guards on
    it before invoking us so this is a runtime narrowing aid for
    mypy rather than a contract surface.
    """
    if interaction.interaction_id is None:
        return
    sender_id = _interaction_sender(interaction)
    asserted_at = interaction.closed_at or interaction.started_at
    try:
        facts = parse_facts_payload(facts_raw)
    except FactsParseError:
        _emit_extraction_failed(1)
        logger.warning(
            "Facts payload parse failed for agent %s (interaction_id=%s); "
            "summary committed, facts did not",
            agent_id, interaction.interaction_id,
        )
        return
    try:
        await store_extracted_facts(
            fact_store,
            facts=facts,
            source_interaction_id=interaction.interaction_id,
            asserted_at=asserted_at,
            session_id=session_id,
            sender_id=sender_id,
        )
    except Exception:
        logger.warning(
            "Failed to store extracted facts for agent %s "
            "(interaction_id=%s)",
            agent_id, interaction.interaction_id, exc_info=True,
        )


def _interaction_sender(interaction: Interaction) -> str | None:
    """Return the first turn's sender id, or ``None`` for tick scopes.

    Used so counterparty facts land on the canonical ``sender_id``
    (the same key the relationship row uses) instead of the LLM's
    display-name spelling — see :func:`store_extracted_facts`.
    """
    if not interaction.turns:
        return None
    payload = interaction.turns[0].payload or {}
    sender = payload.get("sender")
    if isinstance(sender, str) and sender.strip():
        return sender
    return None
