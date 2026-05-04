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
) -> str | None:
    """Return a taxonomised error string, or ``None`` if the event is valid."""
    if len(request.content) > _CHANNEL_CONTENT_MAX_CHARS:
        return (
            f"content exceeds {_CHANNEL_CONTENT_MAX_CHARS} characters "
            f"(got {len(request.content)})"
        )
    if len(request.thread_id) > _CHANNEL_THREAD_ID_MAX_CHARS:
        return (
            f"thread_id exceeds {_CHANNEL_THREAD_ID_MAX_CHARS} characters "
            f"(got {len(request.thread_id)})"
        )
    if len(request.mentions) > _CHANNEL_MAX_MENTIONS:
        return (
            f"mentions list exceeds {_CHANNEL_MAX_MENTIONS} entries "
            f"(got {len(request.mentions)})"
        )
    for i, m in enumerate(request.mentions):
        if not _CHANNEL_PARTICIPANT_ID_RE.match(m):
            return f"mentions[{i}] is not a valid participant id: {m!r}"

    # ``sender_id`` carries a stronger trust claim than ``mentions[]`` (it
    # identifies the alleged author) yet rides the same cleartext gRPC
    # transport — apply the participant-id pattern symmetrically. PR #248
    # deep review trust-boundary asymmetry finding.
    if not _CHANNEL_PARTICIPANT_ID_RE.match(request.sender_id):
        return f"sender_id is not a valid participant id: {request.sender_id!r}"

    # Bound attacker-controlled id lengths. Both fields flow into log lines
    # and the cascade re-wrap; unbounded strings are a slow-burn DoS surface
    # on the cleartext port. PR #248 deep review L finding.
    if len(request.channel_id) > _CHANNEL_ID_MAX_CHARS:
        return (
            f"channel_id exceeds {_CHANNEL_ID_MAX_CHARS} characters "
            f"(got {len(request.channel_id)})"
        )
    if len(request.message_id) > _CHANNEL_MESSAGE_ID_MAX_CHARS:
        return (
            f"message_id exceeds {_CHANNEL_MESSAGE_ID_MAX_CHARS} characters "
            f"(got {len(request.message_id)})"
        )

    expected_prefix = _CHANNEL_TYPE_PREFIXES.get(request.channel_type)
    if expected_prefix is None:
        return (
            f"channel_type {request.channel_type!r} is not one of "
            f"{sorted(_CHANNEL_TYPE_PREFIXES)}"
        )
    if not request.channel_id.startswith(expected_prefix):
        return (
            f"channel_id {request.channel_id!r} prefix disagrees with "
            f"channel_type {request.channel_type!r}"
        )

    # Per ``proto/task.proto`` line 144: receivers MUST treat malformed
    # timestamps as a drop reason. The wire format is RFC 3339 (NOT Unix
    # epoch like ``ChatResponse.timestamp``). Empty is also a drop — the
    # orchestrator's ``ChannelRouter`` always populates this field on
    # publish. PR #248 deep review M finding.
    if parse_channel_timestamp(request.timestamp) is None:
        return f"timestamp is not a valid RFC 3339 string: {request.timestamp!r}"

    return None


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
