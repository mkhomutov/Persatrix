"""RFC 0037 §G (v0.3.12 PR 7) — the leak tripwire at channel egress.

§D and §F close the *verbatim* paths deterministically; the residual risk
is protected text reaching an outgoing message anyway — an entry stamped
with the wrong level, a §E projection that copied source text verbatim,
or a missed injection path.  This module is the **observability** answer
(§G "a smoke detector, not a lock"): on every ``SEND_CHANNEL_MESSAGE``
the :class:`~agents.dispatch.ActionExecutor` checks the outgoing text for
a normalized verbatim span of any entry the turn's §D gate **withheld**,
and on a hit emits a metadata-only ``channel.confidentiality_tripwire``
audit record plus a rate metric.  The message is never blocked and a
tripwire failure never propagates.

Why the watch carries the WITHHELD entries (an as-implemented refinement
of the RFC's manifest sketch): an *admitted* entry's level is ≤ the
acting level by the §D rank rule, and the §B single-channel-turn guard
pins a non-tick publish target to the acting channel — so an admitted
entry can never satisfy §G's "protection level above the target"
condition, and hashing it would be hot-path dead weight.  The withheld
set is exactly the set whose text is above the target; its spans
appearing in output is exactly the bug class §G exists to surface.  A
tick-shaped turn's context is ``public``-floor-gated (its watch may hold
entries above ``public``), and its executor path is origin-less by
design, so tick publishes stay outside the tripwire — the case §G itself
calls vacuous.

The watch is **hash-only**: the turn side
(:mod:`agents.persona_runtime.tripwire_watch`) normalizes each withheld
entry's text and keeps :data:`TRIPWIRE_SPAN_WORDS`-word rolling span
hashes, so no protected text ever rides event metadata, the
``DispatchContext``, the audit trail, or the metric.  The match is
lexical, not semantic: casefold + word tokenization folds case,
whitespace, and punctuation, nothing fuzzier.  The span threshold is the
§G Open-Question-5 conservative default — tuning waits for real tripwire
telemetry.

The audit record follows the ``agent.deliberated`` precedent: the
decision happens in the Python runtime, which has no Go audit RPC, so
the record rides the structured-log egress and the Go-side constant
(``internal/security/audit_event.go``) is a reserved registry entry
keeping the canonical name + severity tables closed.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .observability._metrics_confidentiality import record_tripwire_hit

if TYPE_CHECKING:
    from .persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_EVENT_TRIPWIRE",
    "TRIPWIRE_SPAN_WORDS",
    "TRIPWIRE_WATCH_METADATA_KEY",
    "TripwireHit",
    "TripwireWatch",
    "TripwireWatchEntry",
    "find_tripwire_hits",
    "run_channel_message_tripwire",
    "span_hashes",
    "stamp_tripwire_watch",
    "tripwire_watch_from_event",
]

#: The RFC 0009 audit event name.  Canonical registry + severity
#: classification live Go-side (``AuditChannelConfidentialityTripwire``,
#: security-class); pinned against the Go source by
#: ``test_confidentiality_tripwire.py::TestGoRegistryDriftPin``.
AUDIT_EVENT_TRIPWIRE: Final[str] = "channel.confidentiality_tripwire"

#: Minimum verbatim span length, in normalized words (§G OQ 5's
#: conservative default — long enough that benign phrase collisions are
#: rare, short enough that a copied sentence of protected text cannot
#: slip under it).  Tuned only once real tripwire telemetry exists.
TRIPWIRE_SPAN_WORDS: Final[int] = 8

#: Event-metadata key the turn stamps its watch under (an in-process
#: object, never serialized to the wire — publish metadata is built
#: separately by ``DispatchContext.same_channel_claim``).
TRIPWIRE_WATCH_METADATA_KEY: Final[str] = "confidentiality_tripwire_watch"

# Word tokens = alphanumeric runs (underscore excluded); combined with
# ``str.casefold`` this is the whole §G normalization: case, whitespace,
# and punctuation fold away, nothing fuzzier.
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\W_]+", re.UNICODE)


def span_hashes(
    text: str, *, span_words: int = TRIPWIRE_SPAN_WORDS,
) -> frozenset[str]:
    """Rolling-window hashes of every ``span_words``-word normalized span.

    Two texts share a hash iff they share a verbatim (normalized) span of
    at least ``span_words`` words — substring matching over hashes, so
    the watch can carry an entry's *fingerprint* without its text.  Text
    shorter than the window hashes to the empty set (a copy that short is
    below the §G threshold by definition).
    """
    words = _WORD_RE.findall(text.casefold())
    if len(words) < span_words:
        return frozenset()
    return frozenset(
        hashlib.sha256(
            " ".join(words[i : i + span_words]).encode("utf-8")
        ).hexdigest()
        for i in range(len(words) - span_words + 1)
    )


@dataclass(frozen=True)
class TripwireWatchEntry:
    """One §D-withheld entry's metadata + span fingerprint.

    ``protection_level`` is the entry's stored §A level for a clean
    above-rank withhold, or the sentinel ``"unknown"`` for a rule-(c)
    casualty (the raw corrupted label never rides — unbounded, possibly
    content-bearing).
    """

    tier: str
    entry_id: str
    protection_level: str
    span_hashes: frozenset[str]


@dataclass(frozen=True)
class TripwireWatch:
    """The per-turn tripwire input: the acting classification the §D gate
    resolved (``None`` = the rule-(b) ``public`` floor) and every
    watchable withheld entry."""

    acting: str | None
    entries: tuple[TripwireWatchEntry, ...]


@dataclass(frozen=True)
class TripwireHit:
    """One implicated entry: the §G audit unit."""

    entry: TripwireWatchEntry
    matched_spans: int


def stamp_tripwire_watch(
    metadata: dict[str, object], watch: TripwireWatch | None,
) -> None:
    """Stamp ``watch`` onto an event's metadata for the executor lift.

    ``None`` (nothing withheld — the common case) writes nothing, so an
    unwatched turn costs zero bytes and the executor no-ops.
    """
    if watch is not None:
        metadata[TRIPWIRE_WATCH_METADATA_KEY] = watch


def tripwire_watch_from_event(event: AgentEvent) -> TripwireWatch | None:
    """The tolerant reader behind ``DispatchContext.for_event``: absent or
    non-:class:`TripwireWatch` values (a malformed producer can seed any
    metadata) read as unwatched."""
    value = event.metadata.get(TRIPWIRE_WATCH_METADATA_KEY)
    return value if isinstance(value, TripwireWatch) else None


def find_tripwire_hits(
    watch: TripwireWatch, content: str,
) -> list[TripwireHit]:
    """Pure §G check: every watch entry sharing at least one normalized
    span with ``content``, in watch order."""
    content_hashes = span_hashes(content)
    if not content_hashes:
        return []
    hits: list[TripwireHit] = []
    for entry in watch.entries:
        matched = len(entry.span_hashes & content_hashes)
        if matched:
            hits.append(TripwireHit(entry=entry, matched_spans=matched))
    return hits


def run_channel_message_tripwire(
    *,
    watch: TripwireWatch | None,
    agent_id: str,
    channel_id: str,
    content: str,
) -> None:
    """Run the §G check for one outgoing ``SEND_CHANNEL_MESSAGE``.

    Emits one metadata-only audit record per implicated entry (persona,
    target channel, entry tier/id, the entry's protection level, the
    acting classification, and a match count — **never** the text) plus
    the ``channel.confidentiality.tripwire_hits`` rate metric.  Both
    emits are best-effort and the whole check is exception-proof: the
    message has already been authored and is not blocked (§G), so
    nothing here may fail, block, or reorder the publish.
    """
    if watch is None or not watch.entries or not content:
        return
    with contextlib.suppress(Exception):
        for hit in find_tripwire_hits(watch, content):
            logger.info(
                AUDIT_EVENT_TRIPWIRE,
                extra={
                    "audit": True,
                    "agent_id": agent_id,
                    "channel_id": channel_id,
                    "entry_tier": hit.entry.tier,
                    "entry_id": hit.entry.entry_id,
                    "protection_level": hit.entry.protection_level,
                    "acting_classification": watch.acting,
                    "matched_spans": hit.matched_spans,
                },
            )
            record_tripwire_hit(
                tier=hit.entry.tier,
                protection_level=hit.entry.protection_level,
            )
