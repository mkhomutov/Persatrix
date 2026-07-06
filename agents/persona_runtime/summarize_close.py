"""Summarisation-on-close helper for the persona runtime.

RFC 0020 PR 4 — extracted from
:mod:`agents.persona_runtime.state_persistence` to keep that module
under the 500-line code-file size cap (``scripts/checks/file_size.py``).

:func:`summarize_closed_interaction` runs the combined summarise + extract
LLM call (bounded by timeout + ``MemoryStore.compress`` token budget).  A
single-turn interaction with no inbound message body keeps a cheap
deterministic placeholder; a single-turn interaction that carries message
text is routed through the LLM path so RFC 0026 facts still extract (F-6).

The two-phase close-path TAIL that drives this call —
:func:`~agents.persona_runtime.finalize_close.finalize_closed_interaction`
plus the ``drain_pending_summary_tasks`` / ``maybe_run_janitor`` helpers —
lives in :mod:`agents.persona_runtime.finalize_close` (PR #718 review: split
back out under the size cap).  The relationship-recording half
(``record_closed_interaction`` / ``extract_peer_from_interaction``) lives in
:mod:`agents.persona_runtime.record_close`.

All functions are module-level and ``self``-free so the per-call site in
the mixin stays a one-liner that satisfies the file-size guard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..generated import wallet_pb2 as walletpb
from ..memory.interactions import SUMMARY_UNAVAILABLE_TEXT
from ..memory.store import CompressedView, MemoryEntry, MemoryStore
from ..model_aliases import resolve as resolve_model
from ..observability.metrics import current_agent_id, try_get_instruments
from ..optimization import summarization_model
from ..prompt_loader import load_snippet
from .fact_extractor import (
    FactsParseError,
    build_combined_prompt_suffix,
    emit_envelope_parse_failed,
    split_combined_response,
)

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from ..memory.interactions import Interaction

logger = logging.getLogger(__name__)

# RFC 0020 PR 4 §"Summarisation hook".  The PR-plan (line ~190) pins
# the per-call timeout small enough to keep the close path responsive.
# 30s mirrors the default ``event_timeout`` for persona events; a
# slower model that exceeds it falls through to the fallback summary
# path so a stuck close never wedges the runtime.
SUMMARIZATION_TIMEOUT_SEC: float = 30.0

# RFC 0020 PR 4 cross-RFC pin: the ``MemoryStore.compress`` target
# token budget for the per-interaction summarisation context.  RFC 0020
# §Security caps single-interaction context at 2k tokens to bound LLM
# cost; the value is shared with the abstractive path (RFC 0008 PR 5)
# so the contract does not drift across RFCs.
SUMMARIZATION_TARGET_TOKENS: int = 2000

# Output-token ceiling for the combined summarise + extract LLM call.
# RFC 0020 PR 4 set this to 256 for a *summary-only* call (the prompt
# asks for one short paragraph).  RFC 0026 PR 2 appended the ``facts``
# array to the same envelope without raising the cap, so a multi-fact
# interaction produced a ``{"summary": ..., "facts": [...]}`` envelope
# larger than 256 output tokens, truncated mid-JSON, and lost both
# halves (ISSUE-0054).  1024 covers a one-paragraph summary plus a
# multi-fact array with roughly 2x headroom over a realistic worst
# case.  ``max_tokens`` is a ceiling, not a reservation — typical short
# interactions stop (``end_turn``) well below it, so the raise carries
# no steady-state cost; it only buys tail robustness against truncation.
SUMMARIZATION_MAX_OUTPUT_TOKENS: int = 1024


async def summarize_closed_interaction(
    llm_client: LLMClient,
    agent_id: str,
    interaction: Interaction,
) -> tuple[str, bool, str | None]:
    """Build an LLM-generated summary + extract declarative facts.

    RFC 0026 PR 2 — the summariser prompt is now a **two-output**
    structured prompt: one LLM round-trip returns one JSON envelope
    ``{"summary": "...", "facts": [...]}``.  The summary half feeds
    the existing :meth:`EpisodicMemory.update_episode_summary` write
    (RFC 0020 PR 4); the facts half is serialised back to a JSON list
    string and returned alongside so the orchestrator can dispatch
    :func:`store_extracted_facts` after the summary commits.

    Returns ``(summary_text, failed_bool, facts_raw_or_None)``:

    * ``summary_text`` — the prose summary (or
      :data:`SUMMARY_UNAVAILABLE_TEXT` when ``failed_bool`` is
      ``True``).
    * ``failed_bool`` — ``True`` iff the LLM path failed entirely
      (timeout / exception / empty response).  Mirrors the RFC 0020
      PR 4 contract.
    * ``facts_raw_or_None`` — JSON-serialised list of fact tuples when
      the response parses as the combined envelope; ``None`` when the
      response is plain text (backward-compat path — older mock
      clients and legacy LLM responses without the envelope still
      yield a valid summary write but no facts).
    """
    if interaction.turn_count == 1:
        payload = interaction.turns[0].payload or {}
        single = str(payload.get("summary", "")).strip()
        has_message_text = bool(str(payload.get("text", "")).strip())
        # F-6 (v0.3.1 MT-MEMORY-005 re-run) — a single-turn interaction
        # that carries an inbound message body (``text``) is a real
        # one-message conversation: fall through to the LLM summarise +
        # extract path below so an RFC 0026 fact stated in a one-turn
        # interaction still reaches the facts tier.  Only a content-less
        # single turn keeps the cheap deterministic placeholder — its
        # extractor input would carry no message body, so an LLM call
        # could only ever (correctly) extract nothing.
        if single and not has_message_text:
            # Single-turn placeholder; no facts extracted (the
            # deterministic per-turn shape is not LLM-routed).
            return (
                f"Multi-turn interaction (scope={interaction.scope}, "
                f"turns=1, reason={interaction.close_reason}): "
                f"first[{single}] last[{single}]",
                False,
                None,
            )

    entries = _interaction_to_entries(interaction)
    view: CompressedView = MemoryStore.compress(
        entries,
        target_tokens=SUMMARIZATION_TARGET_TOKENS,
    )
    prompt = _build_summarization_prompt(interaction, view)
    # Summarisation picks its model on a surface separate from
    # create_provider; resolve it here too (RFC 0033 §D) so the alias name
    # never reaches the vendor API. The config field references the
    # ``summarizer`` alias, which resolves to the physical model the same way
    # the factory path does.
    #
    # resolve() is a *startup* validator — it raises SystemExit (a
    # BaseException) on an unknown reference (and, since RFC 0033 Phase 3
    # retired the raw-vendor-ID pass-through, on any non-alias reference).
    # This surface runs per-close on a background task whose caller
    # (finalize_closed_interaction) guards only ``except Exception``, so an
    # unresolvable summarisation model must degrade to the deterministic
    # fallback like every other failure here rather than escape as an
    # uncaught task exception that also skips the failure metric.
    summarization_model_ref = summarization_model()
    try:
        resolved_summarization = resolve_model(summarization_model_ref)
    except SystemExit as exc:
        logger.warning(
            "Summarisation model %r is not resolvable for agent %s "
            "(scope=%s): %s; using fallback",
            summarization_model_ref, agent_id, interaction.scope, exc,
        )
        _emit_summary_failed("model_unresolvable")
        return (SUMMARY_UNAVAILABLE_TEXT, True, None)
    # RFC 0052 PR 4b-ii (OQ #6): on the AUTONOMOUS bounded close — the
    # interaction ``close_notification.py`` marked ``meter_close_summary`` off
    # the typed wire trigger — the summariser call draws an RFC 0023 wallet
    # lease billed to the GOVERNANCE interaction id, so the summary's spend
    # counts toward the mandatory cap: this is the ``N`` of the PR 4a
    # ``1 + N`` reserve (one summary per persona; the soft-budget close fires
    # early precisely so these leases keep hard-cap headroom). CONDITIONAL — an
    # unmarked (human / end-vote / idle) close keeps the pre-4b-ii unleased
    # call signature byte-for-byte (test_summarize_close_metering.py), and an
    # empty wire id (unreachable by CP2 construction) defensively stays
    # unleased rather than lease against no cap with a fail-closed mode.
    lease_kwargs: dict[str, Any] = {}
    if interaction.meter_close_summary and interaction.wire_interaction_id:
        lease_kwargs = {
            "cause": walletpb.CAUSE_CHANNEL_MESSAGE,
            "agent_id": agent_id,
            "interaction_id": interaction.wire_interaction_id,
        }
    elif interaction.meter_close_summary:
        # PR #718 review finding 2: the record is marked for metering but carries
        # no wire interaction id, so the summary below runs UNLEASED and its spend
        # never counts against the mandatory cap the PR 4a ``1 + N`` reserve was
        # carved for. The direction is safe (under-lease, and the reserve is still
        # dark), and CP2 construction makes an empty id unreachable in a
        # homogeneous fleet — but the over-length-id → untracked degradation at
        # ``channel_wire_metadata._INTERACTION_ID_MAX_BYTES`` makes it reachable
        # from a non-Go/compromised producer. Warn rather than fail closed (a
        # lease-less summary beats a dropped one) so the "unreachable" assumption
        # is not a silent hole once the reserve is enforced.
        logger.warning(
            "Close summary marked for metering but has no wire interaction id "
            "for agent %s (scope=%s); running the summary UNLEASED",
            agent_id, interaction.scope,
        )
        _emit_summary_unleased()
    try:
        response = await asyncio.wait_for(
            llm_client.create_message(
                model=resolved_summarization.model,
                # RFC 0033 §G — emit the alias the summariser model came in via
                # (e.g. ``summarizer``) on the span. Since Phase 3 retired the
                # raw-ID pass-through, a resolved reference is always an alias.
                model_alias=resolved_summarization.alias,
                messages=[{"role": "user", "content": prompt}],
                system=load_snippet("episode-summarizer"),
                tools=[],
                max_tokens=SUMMARIZATION_MAX_OUTPUT_TOKENS,
                temperature=0.2,
                **lease_kwargs,
            ),
            timeout=SUMMARIZATION_TIMEOUT_SEC,
        )
    except TimeoutError:
        logger.warning(
            "Summarisation timed out for agent %s (scope=%s); using fallback",
            agent_id, interaction.scope,
        )
        _emit_summary_failed("timeout")
        return (SUMMARY_UNAVAILABLE_TEXT, True, None)
    except Exception as exc:
        logger.warning(
            "Summarisation failed for agent %s (scope=%s): %s",
            agent_id, interaction.scope, exc,
        )
        _emit_summary_failed("llm_error")
        return (SUMMARY_UNAVAILABLE_TEXT, True, None)

    text = (response.text or "").strip()
    if not text:
        logger.warning(
            "Summarisation returned empty text for agent %s (scope=%s); "
            "using fallback",
            agent_id, interaction.scope,
        )
        _emit_summary_failed("empty")
        return (SUMMARY_UNAVAILABLE_TEXT, True, None)
    # Combined-envelope path; plain prose falls through to the
    # backward-compat branch (commit text as summary, facts=None).
    # PR 5b — ``exc.reason`` partitions truncated / missing-summary /
    # invalid-envelope shapes onto a dedicated counter.
    try:
        summary, facts_raw = split_combined_response(text)
    except FactsParseError as exc:
        if exc.reason is not None:
            # ISSUE-0054 — a non-``None`` reason means the model
            # intended a JSON envelope but it is broken (truncated
            # mid-JSON / missing the ``summary`` key / wrong top-level
            # shape).  Committing the raw text as the episode summary
            # stores malformed JSON that degrades episodic recall, so
            # treat it as a summary failure: the janitor owns the row,
            # consistent with the empty / empty_field branches.  Only a
            # ``None`` reason — genuine legacy plain prose — keeps the
            # backward-compat commit below.
            emit_envelope_parse_failed(exc.reason)
            _emit_summary_failed(exc.reason)
            return (SUMMARY_UNAVAILABLE_TEXT, True, None)
        return (text, False, None)
    # PR #340 deep-review S2: a well-formed envelope with an empty
    # ``summary`` field parses cleanly today and commits ``""`` to
    # ``update_episode_summary`` while letting facts dispatch fire
    # against a missing prose half — violating the §G audit ordering
    # "summary always exists before any facts.store row".  Treat the
    # empty-field case as a summary failure consistent with the empty-
    # response branch above; the distinct ``empty_field`` reason lets
    # operators disambiguate "model returned nothing" from "model
    # returned a valid envelope with an empty summary."  Raising
    # ``FactsParseError`` inside :func:`split_combined_response` would
    # be caught by the backward-compat branch above and commit the
    # raw JSON envelope as the summary — worse than today — so the
    # check belongs at the caller.
    if not summary.strip():
        logger.warning(
            "Summarisation returned an empty `summary` field in the "
            "JSON envelope for agent %s (scope=%s); using fallback",
            agent_id, interaction.scope,
        )
        _emit_summary_failed("empty_field")
        return (SUMMARY_UNAVAILABLE_TEXT, True, None)
    return (summary, False, facts_raw)


def _interaction_to_entries(interaction: Interaction) -> list[MemoryEntry]:
    """Project per-turn payloads into ``MemoryEntry`` shape for compress().

    Each turn becomes one entry; importance equals the turn ordinal
    normalised into ``(0, 1]`` so later turns weigh slightly more
    than openers when the compressor has to drop entries.
    """
    total = max(interaction.turn_count, 1)
    entries: list[MemoryEntry] = []
    for idx, turn in enumerate(interaction.turns, start=1):
        payload = turn.payload or {}
        content_parts: list[str] = []
        # ISSUE-0054 — ``text`` (inbound message body) is the load-bearing
        # input for RFC 0026 extraction; ``summary`` is the action envelope.
        for key in ("text", "summary"):
            value = str(payload.get(key, "")).strip()
            if value:
                content_parts.append(value)
        sender = str(payload.get("sender", "")).strip()
        if sender:
            content_parts.append(f"sender={sender}")
        if not content_parts:
            content_parts.append(
                f"event_type={payload.get('event_type', 'unknown')}",
            )
        entries.append(MemoryEntry(
            id=f"turn-{idx}",
            content=" | ".join(content_parts),
            importance=idx / total,
            tags=(),
            created_at=turn.at,
            score=0.0,
        ))
    return entries


def _build_summarization_prompt(
    interaction: Interaction, view: CompressedView,
) -> str:
    """Render the combined summarise + extract prompt body.

    RFC 0026 PR 2 appends the combined-prompt suffix from
    :func:`agents.persona_runtime.fact_extractor.build_combined_prompt_suffix`
    onto the existing RFC 0020 PR 4 summary prompt — one LLM call,
    two structured outputs.  The summary prompt body itself stays
    unchanged so the RFC 0020 PR 4 regression suite remains green.
    """
    return (
        load_snippet("interaction-summarizer") + "\n\n"
        f"Scope: {interaction.scope}\n"
        f"Turns: {interaction.turn_count}\n"
        f"Close reason: {interaction.close_reason}\n"
        f"Tokens (before compression / after): "
        f"{view.tokens_before} / {view.tokens_after}\n"
        f"Entries dropped during compression: {view.entries_dropped}\n\n"
        f"Compressed turns:\n{view.summary}\n"
        + build_combined_prompt_suffix()
    )


def _emit_summary_failed(reason: str) -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    inst.interactions_summary_failed.add(
        1, {"agent_id": current_agent_id(), "reason": reason},
    )


def _emit_summary_unleased() -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    inst.interactions_summary_unleased.add(1, {"agent_id": current_agent_id()})
