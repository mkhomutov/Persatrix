"""
Unit tests for ``agents.channel_validation`` (RFC 0011 PR 4a-i).

PR #248 deep review findings addressed here:

- **M1 + M2** — ``validate_channel_message_event`` returns
  ``tuple[str | None, float | None]`` so the parsed RFC 3339 timestamp
  is the single-source-of-truth output of validation. The servicer no
  longer re-parses, and the bare ``assert`` that pinned the
  "validation succeeded ⇒ timestamp parsed" contract is gone (asserts
  strip under ``python -O``, leaving a latent ``None`` to flow into
  ``AgentEvent.timestamp: float``).

- **M3** — ``error_message`` strings flow back across the wire to the
  orchestrator and almost certainly land in log lines / metric labels.
  Attacker-controlled fields embedded via ``repr()`` were unbounded in
  length and could carry control characters, opening (a) a log-injection
  surface and (b) a slow-burn log-cardinality DoS surface on the
  cleartext gRPC port. ``_safe_repr`` caps length and strips control
  characters at every site that previously used ``f"...{field!r}"``.

These are pure unit tests against the validator; handler-level coverage
lives in ``test_receive_channel_message.py``.
"""

from __future__ import annotations

import pytest

from agents.channel_validation import (
    _safe_repr,
    parse_channel_timestamp,
    validate_channel_message_event,
)
from agents.generated import task_pb2


def _event(**overrides: object) -> task_pb2.ChannelMessageEvent:
    fields: dict[str, object] = {
        "message_id": "msg-001",
        "channel_id": "group:general",
        "channel_type": "group",
        "sender_id": "iron-fox",
        "content": "hello",
        "timestamp": "2026-05-04T00:00:00Z",
        "thread_id": "",
        "mentions": [],
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


# ─── M1 + M2: validator returns parsed timestamp ───────────


class TestValidatorReturnShape:
    def test_returns_none_error_and_parsed_timestamp_on_valid(self):
        err, ts = validate_channel_message_event(
            _event(timestamp="2026-05-04T12:34:56Z"),
        )
        assert err is None
        assert ts == pytest.approx(1777898096.0, abs=1.0)

    def test_returns_error_and_none_timestamp_on_invalid(self):
        err, ts = validate_channel_message_event(_event(content="x" * 4001))
        assert err is not None
        assert "content" in err
        assert ts is None

    def test_returns_error_and_none_timestamp_on_bad_timestamp(self):
        # When the timestamp itself is the failing field, parse must not
        # leak a value alongside the error.
        err, ts = validate_channel_message_event(_event(timestamp="not-rfc3339"))
        assert err is not None
        assert "timestamp" in err
        assert ts is None


# ─── M3: _safe_repr caps length + strips control characters ──


class TestSafeRepr:
    def test_short_value_renders_with_quotes(self):
        # Quoted form preserves the visual "this is a string" cue from
        # the original ``!r`` so taxonomies remain readable.
        assert _safe_repr("alice") == "'alice'"

    def test_long_value_truncates_with_ellipsis(self):
        rendered = _safe_repr("x" * 200)
        # 32-char cap (excluding quote-delimiters and ellipsis suffix).
        assert len(rendered) <= 40
        assert rendered.endswith("…'")

    def test_control_characters_replaced(self):
        rendered = _safe_repr("a\nb\rc\td")
        # No raw newline/carriage-return/tab — would otherwise enable
        # log-injection (e.g. forging a fake log line on the next line).
        assert "\n" not in rendered
        assert "\r" not in rendered
        assert "\t" not in rendered

    def test_empty_value_round_trips(self):
        # Empty input is meaningful in some validation paths (empty
        # ``sender_id``); rendering must remain unambiguous.
        assert _safe_repr("") == "''"


# ─── M3: validator error messages are bounded + sanitised ──


class TestErrorMessageBounded:
    def test_oversized_mention_id_does_not_leak_full_value(self):
        attacker = "X" * 5000
        err, _ = validate_channel_message_event(_event(mentions=[attacker]))
        assert err is not None
        # Message must not contain the full 5000-char blob.
        assert len(err) < 200
        assert attacker not in err

    def test_oversized_sender_id_does_not_leak_full_value(self):
        # ``sender_id`` fails the participant-id regex once it contains
        # uppercase, so the field gets ``_safe_repr``'d into the message.
        attacker = "A" * 5000
        err, _ = validate_channel_message_event(_event(sender_id=attacker))
        assert err is not None
        assert len(err) < 200
        assert attacker not in err

    def test_control_chars_in_attacker_field_are_stripped(self):
        # Newlines in error_message would let an attacker forge log lines
        # downstream. Validate at the earliest field that takes a verbatim
        # value (mention id pattern check). The sanitiser replaces each
        # control byte with its ``\xNN`` escape so the original bytes are
        # never present in the rendered string.
        err, _ = validate_channel_message_event(
            _event(mentions=["bad\nINJECTED LINE"]),
        )
        assert err is not None
        assert "\n" not in err
        assert "\r" not in err
        # Newline must be rendered as the escape sequence, not the raw byte.
        assert "\\x0a" in err


# ─── parse_channel_timestamp coverage (regression guard) ──


class TestParseChannelTimestamp:
    def test_accepts_z_suffix(self):
        assert parse_channel_timestamp("2026-05-04T00:00:00Z") is not None

    def test_accepts_explicit_offset(self):
        assert parse_channel_timestamp("2026-05-04T00:00:00+00:00") is not None

    def test_rejects_empty(self):
        assert parse_channel_timestamp("") is None

    def test_rejects_garbage(self):
        assert parse_channel_timestamp("not-a-timestamp") is None
