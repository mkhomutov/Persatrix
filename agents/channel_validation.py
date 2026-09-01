"""
Defensive validation for ``ChannelMessageEvent`` on the receiver side.

Extracted from ``agents/server_servicers.py`` (RFC 0011 PR 4a-i) to keep
that module under the 500-line review-friendly cap. Mirrors the bounds
documented in ``proto/task.proto`` so the receiver-side defence-in-depth
check matches the sender-side contract — drift between these constants
and the proto comment block is a regression; update both.

The orchestrator's ``ChannelRouter`` enforces the same bounds at the
publish boundary, but the receiver cannot trust the cleartext gRPC
transport in v0.3.0 (see ``agents/server.py`` TLS TODO and PR #246
deep review security finding on sender-spoofing).
"""

from __future__ import annotations

import re
from datetime import datetime

from .generated import task_pb2

__all__ = [
    "parse_channel_timestamp",
    "validate_channel_message_dict",
    "validate_channel_message_event",
]


# Cap rendered length of attacker-controlled fields embedded in
# ``error_message`` strings. Those strings flow back across the wire and
# almost certainly land in orchestrator logs / metric labels; unbounded
# bytes are a slow-burn log-cardinality DoS surface and (with control
# characters) a log-injection surface. PR #248 deep review M finding.
_ERROR_MESSAGE_FIELD_MAX_CHARS = 32

# Strip ASCII control characters (``\x00``-``\x1f`` and ``\x7f``) when
# rendering attacker-controlled fields into ``error_message``. Matters
# most for ``\n``/``\r``/``\t`` which would let an attacker forge a fake
# log line on the next-line boundary downstream. Note: not strictly
# necessary if every consumer escapes its own log lines, but defensible
# defence-in-depth at the boundary that produces the string.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_repr(value: str, *, max_len: int = _ERROR_MESSAGE_FIELD_MAX_CHARS) -> str:
    """Render ``value`` for inclusion in ``error_message`` strings.

    Caps length at ``max_len`` (with an ellipsis suffix on truncation)
    and strips ASCII control characters. Replaces the previous use of
    ``repr()`` / ``f"...{field!r}"`` everywhere a wire-side field is
    embedded into an ``error_message`` that the orchestrator may log.
    PR #248 deep review M finding (log-injection / log-cardinality DoS).

    The quoted form is preserved so taxonomies remain visually unambiguous
    (operators reading wire traces continue to see ``'...'`` deliminators).
    """
    sanitized = _CONTROL_CHARS_RE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", value)
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len] + "…"
    return f"'{sanitized}'"


_CHANNEL_CONTENT_MAX_CHARS = 4000
_CHANNEL_THREAD_ID_MAX_CHARS = 128
_CHANNEL_MAX_MENTIONS = 10
# Bound the cleartext-port DoS surface for fields that flow into log lines
# and the cascade re-wrap. The proto does not pin these explicitly today;
# values chosen to exceed any plausible legitimate id while still capping
# attacker-controlled string length. PR #248 deep review M/L findings.
_CHANNEL_ID_MAX_CHARS = 256
_CHANNEL_MESSAGE_ID_MAX_CHARS = 64
# RFC 0011 PR 4b: closed vocabulary for the per-recipient response policy
# carried on the wire. The orchestrator filters `respond: never` members
# upstream of dispatch (see ``internal/channels/router.go::fanout``), so a
# `never` value reaching the receiver is malformed and rejected.
_CHANNEL_RESPOND_POLICIES = {"when_mentioned", "always"}
# Canonical participant-ID pattern (matches ``^[a-z0-9][a-z0-9-]*[a-z0-9]$``
# from ``.github/copilot-instructions.md``); pinned here rather than imported
# from a Go-side validator because the receiver runs in Python with no
# direct dep on ``internal/channels``.
_CHANNEL_PARTICIPANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
# The broadcast sentinel (RFC 0030 relevance amendment Tier A, decision D3): a
# ``mentions`` entry of ``@everyone`` addresses the whole room and disables the
# directed-elsewhere filter. It is NOT a participant id (the leading ``@`` fails
# ``_CHANNEL_PARTICIPANT_ID_RE``), so it must be carved out of mention-id
# validation or an inbound broadcast is rejected at the envelope boundary —
# before the response gate that special-cases it ever runs (ISSUE-0094). Pinned
# locally for the same reason the regex is (hot-path import-light, no dep on the
# receiver gate); mirrors ``agents.response_gate.MENTION_EVERYONE`` and
# ``internal/channels/channels.go``'s ``MentionEveryone`` — a drift-guard test
# (``test_channel_validation.py``) keeps the three in lock-step. The carve-out
# is scoped to ``mentions`` only: ``sender_id`` carries a stronger trust claim
# and still rejects the sentinel.
_MENTION_EVERYONE = "@everyone"

# RFC 0052 §B/§D — the orchestrator dispatches the convene (opening) and
# synthesis (closing) FORCED TURNS under SYNTHETIC sender ids that deliberately
# contain a reserved ``:`` so they can never collide with a real participant id
# (Go ``internal/channels/convene.go``'s ``ConveneDispatchSenderID`` and
# ``synthesis_close.go``'s ``SynthesisDispatchSenderID``; the ``:`` is reserved
# by the canonical-address grammar and forbidden by ``_CHANNEL_PARTICIPANT_ID_RE``
# — ``convene_test.go`` pins that the sentinel is deliberately NOT a valid
# participant id, so it can never equal an agent id and trip the receiver's
# self-sender defence). They are reserved ORCHESTRATOR CONTROL senders, not
# participant ids, so — exactly as ``@everyone`` is carved out of mention
# validation — they must be carved out of ``sender_id`` validation, or the
# receiver refuses the directive at this envelope boundary BEFORE the
# forced-turn admit (``agents.response_gate``, which keys on the convene /
# synthesis MARKERS, not this string) ever runs, and a convened channel
# 202s-then-does-nothing on an unattended channel — the silent-runaway class the
# RFC's safety contract exists to catch. The carve-out is a BOUNDED, enumerated
# set (like ``@everyone``): a spoofed sender on the cleartext gRPC port (PR #248)
# gains nothing a normal message would not — the self-sender defence still keys
# on real agent ids — and the set is pinned against the Go source of truth by
# ``test_channel_validation_dispatch_senders.py``. Scoped to ``sender_id`` only.
_CONVENE_DISPATCH_SENDER_ID = "orchestrator:convene"
_SYNTHESIS_DISPATCH_SENDER_ID = "orchestrator:synthesis"
_RESERVED_DISPATCH_SENDER_IDS = frozenset(
    {_CONVENE_DISPATCH_SENDER_ID, _SYNTHESIS_DISPATCH_SENDER_ID}
)
# Channel-id prefix ↔ channel_type agreement table (RFC 0011 §B). Receivers
# MUST reject mismatches as malformed (PR #246 deep review security finding).
_CHANNEL_TYPE_PREFIXES = {
    "group": "group:",
    "dm": "dm:",
    "thread": "thread:",
}


def validate_channel_message_event(
    request: task_pb2.ChannelMessageEvent,
) -> tuple[str | None, float | None]:
    """Validate a wire-side ``ChannelMessageEvent``.

    Returns ``(error_message, parsed_timestamp)`` where exactly one side is
    populated:

    - On success: ``(None, <unix epoch seconds>)`` — the parsed RFC 3339
      timestamp is the single-source-of-truth output, eliminating the
      double-parse/assert hazard at the call site (PR #248 deep review
      M findings combined). Callers MUST use this value rather than
      re-parsing ``request.timestamp``.
    - On failure: ``(<taxonomised error string>, None)``.

    Attacker-controlled fields embedded in the error string are rendered
    via ``_safe_repr`` (length-capped, control-char-stripped) so the
    string is safe to log without enabling log-injection or unbounded
    log-cardinality on the cleartext gRPC port.
    """
    if len(request.content) > _CHANNEL_CONTENT_MAX_CHARS:
        return (
            f"content exceeds {_CHANNEL_CONTENT_MAX_CHARS} characters (got {len(request.content)})"
        ), None
    if len(request.thread_id) > _CHANNEL_THREAD_ID_MAX_CHARS:
        return (
            f"thread_id exceeds {_CHANNEL_THREAD_ID_MAX_CHARS} characters "
            f"(got {len(request.thread_id)})"
        ), None
    if len(request.mentions) > _CHANNEL_MAX_MENTIONS:
        return (
            f"mentions list exceeds {_CHANNEL_MAX_MENTIONS} entries (got {len(request.mentions)})"
        ), None
    for i, m in enumerate(request.mentions):
        if m == _MENTION_EVERYONE:
            continue  # broadcast sentinel (D3), not a participant id — ISSUE-0094
        if not _CHANNEL_PARTICIPANT_ID_RE.match(m):
            return f"mentions[{i}] is not a valid participant id: {_safe_repr(m)}", None
    # ``floor_mentions`` (floor-capable-directedness amendment, v0.3.8) is the
    # orchestrator-resolved subset of ``mentions`` and rides the same
    # cleartext port — mirror the mentions bounds. No ``@everyone`` carve-out
    # here: the sentinel is never a member id, so the resolver never emits it;
    # an inbound sentinel (or any pattern violation) is a malformed or spoofed
    # producer and is rejected before the gate consumes the list as its
    # suppression basis. The subset relation itself is enforced too: the
    # resolver computes an intersection, so a conforming producer satisfies
    # it by construction, and rejecting a violation restores the amendment's
    # skew invariant against *spoofed* producers — a subset of ``mentions``
    # can only ever narrow suppression relative to the raw basis, so a
    # fabricated ``floor_mentions`` cannot silence a participant the raw
    # mentions would not already have silenced. (The converse spoof — flag
    # true with an empty list, widening admission — stays accepted:
    # resolved-empty is the motivating human-mention case and is
    # unverifiable here; the amendment's trust-extension paragraph owns
    # that residue.) The entry cap still fires first: the subset rule
    # alone cannot bound the list, since one legitimate mention id may be
    # repeated without limit.
    if len(request.floor_mentions) > _CHANNEL_MAX_MENTIONS:
        return (
            f"floor_mentions list exceeds {_CHANNEL_MAX_MENTIONS} entries "
            f"(got {len(request.floor_mentions)})"
        ), None
    raw_mentions = set(request.mentions)
    for i, m in enumerate(request.floor_mentions):
        if not _CHANNEL_PARTICIPANT_ID_RE.match(m):
            return (f"floor_mentions[{i}] is not a valid participant id: {_safe_repr(m)}"), None
        if m not in raw_mentions:
            return f"floor_mentions[{i}] not in mentions: {_safe_repr(m)}", None

    # ``sender_id`` carries a stronger trust claim than ``mentions[]`` (it
    # identifies the alleged author) yet rides the same cleartext gRPC
    # transport — apply the participant-id pattern symmetrically. PR #248
    # deep review trust-boundary asymmetry finding.
    if (
        request.sender_id not in _RESERVED_DISPATCH_SENDER_IDS
        and not _CHANNEL_PARTICIPANT_ID_RE.match(request.sender_id)
    ):
        return (f"sender_id is not a valid participant id: {_safe_repr(request.sender_id)}"), None

    # Bound attacker-controlled id lengths. Both fields flow into log lines
    # and the cascade re-wrap; unbounded strings are a slow-burn DoS surface
    # on the cleartext port. PR #248 deep review L finding.
    if len(request.channel_id) > _CHANNEL_ID_MAX_CHARS:
        return (
            f"channel_id exceeds {_CHANNEL_ID_MAX_CHARS} characters (got {len(request.channel_id)})"
        ), None
    if len(request.message_id) > _CHANNEL_MESSAGE_ID_MAX_CHARS:
        return (
            f"message_id exceeds {_CHANNEL_MESSAGE_ID_MAX_CHARS} characters "
            f"(got {len(request.message_id)})"
        ), None

    expected_prefix = _CHANNEL_TYPE_PREFIXES.get(request.channel_type)
    if expected_prefix is None:
        return (
            f"channel_type {_safe_repr(request.channel_type)} is not one of "
            f"{sorted(_CHANNEL_TYPE_PREFIXES)}"
        ), None
    if not request.channel_id.startswith(expected_prefix):
        return (
            f"channel_id {_safe_repr(request.channel_id)} prefix disagrees with "
            f"channel_type {_safe_repr(request.channel_type)}"
        ), None

    # Per ``proto/task.proto`` line 144: receivers MUST treat malformed
    # timestamps as a drop reason. The wire format is RFC 3339 (NOT Unix
    # epoch like ``ChatResponse.timestamp``). Empty is also a drop — the
    # orchestrator's ``ChannelRouter`` always populates this field on
    # publish. PR #248 deep review M finding (single-source-of-truth):
    # the parsed value is propagated to the caller so the constructor
    # site does not re-parse / cannot diverge.
    publish_ts = parse_channel_timestamp(request.timestamp)
    if publish_ts is None:
        return (f"timestamp is not a valid RFC 3339 string: {_safe_repr(request.timestamp)}"), None

    # RFC 0011 PR 4b: per-recipient ``respond_policy`` MUST be one of the
    # closed vocabulary that the response gate understands. ``never`` is
    # rejected here — the orchestrator filters those members upstream of
    # dispatch, so its presence on the wire signals malformed routing.
    # An empty string is rejected too: the gate requires the policy and
    # would otherwise have to fail-closed on a missing dimension.
    if request.respond_policy not in _CHANNEL_RESPOND_POLICIES:
        return (
            f"respond_policy {_safe_repr(request.respond_policy)} is not one of "
            f"{sorted(_CHANNEL_RESPOND_POLICIES)}"
        ), None

    # Optional ``thread_parent_sender_id``: empty for non-thread events
    # (and benign for thread events when the parent has been pruned).
    # When non-empty it MUST be a valid participant id — same trust
    # boundary as ``sender_id`` and ``mentions[]``.
    if request.thread_parent_sender_id and not _CHANNEL_PARTICIPANT_ID_RE.match(
        request.thread_parent_sender_id,
    ):
        return (
            f"thread_parent_sender_id is not a valid participant id: "
            f"{_safe_repr(request.thread_parent_sender_id)}"
        ), None

    return None, publish_ts


def validate_channel_message_dict(
    msg: dict,
    *,
    channel_type: str,
) -> tuple[str | None, float | None]:
    """Validate a wire-side channel message in JSON / dict form.

    Sibling of :func:`validate_channel_message_event`; the dict variant
    drives the on-startup catch-up fetcher
    (:mod:`agents.channel_catchup`) so the REST/JSON ingest seam
    enforces the same defense-in-depth bounds the cleartext gRPC ingest
    seam already does. PR-265 deep-review L1.

    The two functions deliberately share module-level constants
    (``_CHANNEL_CONTENT_MAX_CHARS``, ``_CHANNEL_PARTICIPANT_ID_RE``,
    ``_CHANNEL_TYPE_PREFIXES``, …) so drift between the live-path and
    catch-up-path bounds is impossible by construction; a future PR
    that bumps any cap updates both call sites in one edit.

    Wire-shape gaps relative to ``ChannelMessageEvent`` (the proto):

    * ``respond_policy`` — not on the JSON wire shape
      (``internal/server/channel_types.go::channelMessageResponse``);
      the catch-up fetcher resolves it from the membership endpoint
      via ``channel_catchup_discovery.resolve_respond_policy`` and
      validates the result there.
    * ``thread_parent_sender_id`` — not on the JSON wire shape (PR-265
      review L2 documents the asymmetry); validation is a no-op for
      this field on the catch-up path.
    * ``channel_type`` — not on the message itself. The catch-up
      fetcher passes the parent channel's ``channel_type`` so the
      ``channel_type`` ↔ ``channel_id`` prefix-agreement check still
      runs.

    Returns ``(error_message, parsed_timestamp)`` with the same
    contract as :func:`validate_channel_message_event` so call sites
    can structure their happy/sad paths symmetrically.
    """
    sender_id = msg.get("sender_id")
    if not isinstance(sender_id, str):
        return "sender_id is not a string", None
    content = msg.get("content", "")
    if not isinstance(content, str):
        return "content is not a string", None
    if len(content) > _CHANNEL_CONTENT_MAX_CHARS:
        return (
            f"content exceeds {_CHANNEL_CONTENT_MAX_CHARS} characters (got {len(content)})"
        ), None

    thread_id = msg.get("thread_id") or ""
    if not isinstance(thread_id, str):
        return "thread_id is not a string", None
    if len(thread_id) > _CHANNEL_THREAD_ID_MAX_CHARS:
        return (
            f"thread_id exceeds {_CHANNEL_THREAD_ID_MAX_CHARS} characters (got {len(thread_id)})"
        ), None

    mentions_raw = msg.get("mentions") or []
    if not isinstance(mentions_raw, list):
        return "mentions is not a list", None
    if len(mentions_raw) > _CHANNEL_MAX_MENTIONS:
        return (
            f"mentions list exceeds {_CHANNEL_MAX_MENTIONS} entries (got {len(mentions_raw)})"
        ), None
    for i, m in enumerate(mentions_raw):
        if m == _MENTION_EVERYONE:
            continue  # broadcast sentinel (D3), not a participant id — ISSUE-0094
        if not isinstance(m, str) or not _CHANNEL_PARTICIPANT_ID_RE.match(m):
            rendered = _safe_repr(m if isinstance(m, str) else str(m))
            return f"mentions[{i}] is not a valid participant id: {rendered}", None

    if sender_id not in _RESERVED_DISPATCH_SENDER_IDS and not _CHANNEL_PARTICIPANT_ID_RE.match(
        sender_id
    ):
        return (f"sender_id is not a valid participant id: {_safe_repr(sender_id)}"), None

    channel_id = msg.get("channel_id")
    if not isinstance(channel_id, str):
        return "channel_id is not a string", None
    if len(channel_id) > _CHANNEL_ID_MAX_CHARS:
        return (
            f"channel_id exceeds {_CHANNEL_ID_MAX_CHARS} characters (got {len(channel_id)})"
        ), None

    message_id = msg.get("id", "")
    if not isinstance(message_id, str):
        return "message_id is not a string", None
    if len(message_id) > _CHANNEL_MESSAGE_ID_MAX_CHARS:
        return (
            f"message_id exceeds {_CHANNEL_MESSAGE_ID_MAX_CHARS} characters (got {len(message_id)})"
        ), None
    if not message_id.strip():
        # ISSUE-0130 (b): an id-less row is not merely odd, it is a row the
        # replay span identity cannot name — and the identity is
        # all-or-nothing by design (a digest over the identified SUBSET
        # would let two different spans collide and cost the second its
        # memory).  One blank id therefore disarmed the re-derivation guard
        # for that whole window, on EVERY boot, since the condition is
        # deterministic: the growth curve back, with only a WARN.
        #
        # Dropping the row here is the cheap end of that trade and is safe
        # precisely because it IS deterministic — every boot drops the same
        # row, so the surviving span's digest is identical across boots.
        # `messages.id` is the store's primary key, so a real orchestrator
        # never sends this.
        return "message_id is empty", None

    expected_prefix = _CHANNEL_TYPE_PREFIXES.get(channel_type)
    if expected_prefix is None:
        return (
            f"channel_type {_safe_repr(channel_type)} is not one of "
            f"{sorted(_CHANNEL_TYPE_PREFIXES)}"
        ), None
    if not channel_id.startswith(expected_prefix):
        return (
            f"channel_id {_safe_repr(channel_id)} prefix disagrees with "
            f"channel_type {_safe_repr(channel_type)}"
        ), None

    raw_ts = msg.get("timestamp", "")
    if not isinstance(raw_ts, str):
        return "timestamp is not a string", None
    publish_ts = parse_channel_timestamp(raw_ts)
    if publish_ts is None:
        return (f"timestamp is not a valid RFC 3339 string: {_safe_repr(raw_ts)}"), None

    return None, publish_ts


def parse_channel_timestamp(value: str) -> float | None:
    """Parse RFC 3339 ``value`` to Unix epoch seconds; return ``None`` if invalid.

    Accepts the trailing ``Z`` UTC marker (proto example
    ``"2026-05-04T00:00:00Z"``) which ``datetime.fromisoformat`` only
    handles natively from Python 3.11+. Empty input is invalid.

    RFC 3339 §5.6 mandates a ``time-offset`` (``Z`` or ``±HH:MM``) on every
    ``date-time``. A naive string like ``"2026-05-04T00:00:00"`` parses
    successfully via ``datetime.fromisoformat`` but produces a *naive*
    ``datetime``; the subsequent ``.timestamp()`` call then converts via
    the *host* timezone, silently shifting the publish time by however
    many hours the receiver is offset from UTC. Receivers MUST reject
    naive input rather than admit a host-TZ-dependent value. PR #248
    deep review nice-to-have finding.
    """
    if not value:
        return None
    # ``Z`` → ``+00:00`` for cross-version compatibility; 3.11+ accepts
    # ``Z`` directly but the explicit substitution is cheap and clear.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    # Reject naive datetimes: an RFC 3339 ``date-time`` MUST carry an
    # offset, so a naive parse means the input violated the contract.
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()
