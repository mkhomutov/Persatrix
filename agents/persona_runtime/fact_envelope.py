"""Combined summarize + extract response parser (RFC 0026 PR 2 + PR 5b).

Split out of :mod:`agents.persona_runtime.fact_extractor` so the parser
+ its observability surface stays separable from the storage dispatch.
The extractor module re-exports the public names below so existing
imports continue to work.

The parser is pure (no I/O, no metrics emission) — the
``agent.facts.envelope_parse_failed`` counter is emitted by the
*caller* in :mod:`agents.persona_runtime.summarize_close` once the
:class:`FactsParseError.reason` slot routes it to the right
``reason=`` attribute.  Keeping the parser side-effect-free means
tests can pin the reason mapping deterministically with no OTel
fixture wiring.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = [
    "FactsParseError",
    "parse_facts_payload",
    "split_combined_response",
]


class FactsParseError(ValueError):
    """Raised when the combined LLM response can not be split into a
    summary + facts pair, or when the facts payload is not a list of
    dict tuples.

    The caller commits the summary half (when available) and
    increments ``agent.facts.extraction_failed`` — RFC 0026 §Phase 1
    step 4 rollback policy.

    ``reason`` (PR 5b) routes to the new
    ``agent.facts.envelope_parse_failed`` counter:

    * ``None`` — plain-prose backward-compat / empty input.  Counter
      stays quiet (the empty path is already signalled upstream on
      ``agent.interactions.summary.failed{reason=empty}``).
    * ``"truncated"`` — text starts with ``{``/``[`` but JSON parsing
      fails — the motivating case under the combined-prompt
      ``max_tokens=256`` cap.
    * ``"missing_summary"`` — parses as an object but lacks the
      load-bearing ``summary`` key (or the value has the wrong type).
    * ``"invalid_envelope"`` — parses as JSON but the top level is
      not an object (e.g., the model emitted a bare list).
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


def split_combined_response(raw: str) -> tuple[str, str]:
    """Split the combined LLM response into ``(summary, facts_json)``.

    Returns a 2-tuple where:

    * ``summary`` is the prose summary string (load-bearing — its
      absence is treated as an LLM failure, not a facts-parse
      failure).
    * ``facts_json`` is the **serialised** JSON list of fact tuples.
      Re-serialising means :func:`parse_facts_payload` remains the
      single source of truth for fact-tuple shape validation; the
      caller does not have to reach into the dict.

    Raises :class:`FactsParseError` if the envelope itself does not
    parse / has the wrong top-level shape / is missing ``summary``.
    A missing ``facts`` key is **not** an error — it is treated as
    ``[]`` so the LLM can omit the key on short interactions.

    PR 5b — the raised :class:`FactsParseError` carries a ``reason``
    slot so the caller can route the four failure shapes to distinct
    counter buckets (or skip the counter entirely on the plain-prose
    backward-compat path).
    """
    text = (raw or "").strip()
    if not text:
        # Direct-caller fence; production strips + early-returns
        # upstream and the empty path already signals on
        # ``interactions.summary.failed{reason=empty}``.
        raise FactsParseError("combined response is empty")
    # A leading ``{`` or ``[`` is the contract every modern LLM
    # response follows; plain prose lacks it.  Used to keep the
    # envelope counter quiet on the backward-compat path.
    looks_like_envelope = text[:1] in {"{", "["}
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        reason = "truncated" if looks_like_envelope else None
        raise FactsParseError(
            f"combined response is not JSON: {exc}", reason=reason,
        ) from exc
    if not isinstance(envelope, Mapping):
        raise FactsParseError(
            f"combined response must be a JSON object, got "
            f"{type(envelope).__name__}",
            reason="invalid_envelope",
        )
    if "summary" not in envelope:
        raise FactsParseError(
            "combined response missing required `summary` key",
            reason="missing_summary",
        )
    summary = envelope["summary"]
    if not isinstance(summary, str):
        raise FactsParseError(
            f"`summary` must be a string, got {type(summary).__name__}",
            reason="missing_summary",
        )
    facts = envelope.get("facts", [])
    facts_json = json.dumps(facts)
    return summary, facts_json


def parse_facts_payload(raw: str) -> list[dict[str, Any]]:
    """Parse the ``facts`` half of the combined response into tuple dicts.

    Returns ``[]`` for the empty-list path (the expected common case
    on short interactions).  Raises :class:`FactsParseError` on
    malformed JSON / non-list payloads / list elements that aren't
    dicts.

    The shape check stops at "each element is a dict" — per-tuple
    field validation (subject / predicate / object presence,
    predicate allowlist, certainty range) lives in
    :func:`agents.persona_runtime.fact_extractor.store_extracted_facts`
    so per-tuple failures can increment
    ``agent.facts.extraction_failed`` without aborting the batch.
    """
    text = (raw or "").strip()
    if not text:
        raise FactsParseError("facts payload is empty")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FactsParseError(f"facts payload is not JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise FactsParseError(
            f"facts payload must be a JSON list, got {type(payload).__name__}",
        )
    for idx, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise FactsParseError(
                f"facts[{idx}] must be a JSON object, got "
                f"{type(item).__name__}",
            )
    return [dict(item) for item in payload]
