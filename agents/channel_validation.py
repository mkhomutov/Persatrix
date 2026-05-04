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

from .generated import task_pb2

__all__ = ["validate_channel_message_event"]


_CHANNEL_CONTENT_MAX_CHARS = 4000
_CHANNEL_THREAD_ID_MAX_CHARS = 128
_CHANNEL_MAX_MENTIONS = 10
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
    return None
