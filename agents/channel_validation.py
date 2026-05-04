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

__all__ = ["validate_channel_message_event", "parse_channel_timestamp"]


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
# Canonical participant-ID pattern (matches ``^[a-z0-9][a-z0-9-]*[a-z0-9]$``
# from ``.github/copilot-instructions.md``); pinned here rather than imported
# from a Go-side validator because the receiver runs in Python with no
# direct dep on ``internal/channels``.
_CHANNEL_PARTICIPANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
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
            f"content exceeds {_CHANNEL_CONTENT_MAX_CHARS} characters "
            f"(got {len(request.content)})"
        ), None
    if len(request.thread_id) > _CHANNEL_THREAD_ID_MAX_CHARS:
        return (
            f"thread_id exceeds {_CHANNEL_THREAD_ID_MAX_CHARS} characters "
            f"(got {len(request.thread_id)})"
        ), None
    if len(request.mentions) > _CHANNEL_MAX_MENTIONS:
        return (
            f"mentions list exceeds {_CHANNEL_MAX_MENTIONS} entries "
            f"(got {len(request.mentions)})"
        ), None
    for i, m in enumerate(request.mentions):
        if not _CHANNEL_PARTICIPANT_ID_RE.match(m):
            return f"mentions[{i}] is not a valid participant id: {_safe_repr(m)}", None

    # ``sender_id`` carries a stronger trust claim than ``mentions[]`` (it
    # identifies the alleged author) yet rides the same cleartext gRPC
    # transport — apply the participant-id pattern symmetrically. PR #248
    # deep review trust-boundary asymmetry finding.
    if not _CHANNEL_PARTICIPANT_ID_RE.match(request.sender_id):
        return (
            f"sender_id is not a valid participant id: {_safe_repr(request.sender_id)}"
        ), None

    # Bound attacker-controlled id lengths. Both fields flow into log lines
    # and the cascade re-wrap; unbounded strings are a slow-burn DoS surface
    # on the cleartext port. PR #248 deep review L finding.
    if len(request.channel_id) > _CHANNEL_ID_MAX_CHARS:
        return (
            f"channel_id exceeds {_CHANNEL_ID_MAX_CHARS} characters "
            f"(got {len(request.channel_id)})"
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
        return (
            f"timestamp is not a valid RFC 3339 string: {_safe_repr(request.timestamp)}"
        ), None

    return None, publish_ts


def parse_channel_timestamp(value: str) -> float | None:
    """Parse RFC 3339 ``value`` to Unix epoch seconds; return ``None`` if invalid.

    Accepts the trailing ``Z`` UTC marker (proto example
    ``"2026-05-04T00:00:00Z"``) which ``datetime.fromisoformat`` only
    handles natively from Python 3.11+. Empty input is invalid.
    """
    if not value:
        return None
    try:
        # ``Z`` → ``+00:00`` for cross-version compatibility; 3.11+ accepts
        # ``Z`` directly but the explicit substitution is cheap and clear.
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None
