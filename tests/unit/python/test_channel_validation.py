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
        # RFC 0011 PR 4b: per-recipient policy is required by the
        # validator. Default to ``always`` so existing tests keep
        # passing; cases that exercise the policy field override.
        "respond_policy": "always",
        "thread_parent_sender_id": "",
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

    # ─── Naive (no-offset) RFC 3339 rejection (PR #248 NTH) ──
    #
    # RFC 3339 §5.6 requires every ``date-time`` to carry a ``time-offset``
    # (either ``Z`` or ``±HH:MM``). A naive string like
    # ``"2026-05-04T00:00:00"`` has no offset; ``datetime.fromisoformat``
    # parses it as a *naive* ``datetime`` and ``.timestamp()`` then converts
    # via the *host* timezone — silently shifting the publish timestamp by
    # whatever the receiver's TZ happens to be. The proto contract is
    # RFC 3339 (with offset), so receivers MUST reject naive input rather
    # than admitting host-TZ-dependent values.
    def test_rejects_naive_no_offset(self):
        assert parse_channel_timestamp("2026-05-04T00:00:00") is None

    def test_rejects_naive_with_fractional_seconds(self):
        # Fractional-seconds form must still require the offset.
        assert parse_channel_timestamp("2026-05-04T00:00:00.123") is None

    def test_accepts_negative_offset(self):
        # Symmetry guard: a non-UTC offset is valid RFC 3339; only the
        # missing-offset case is rejected.
        assert parse_channel_timestamp("2026-05-04T00:00:00-05:00") is not None


# ─── RFC 0011 PR 4b: respond_policy + thread_parent_sender_id ───


class TestRespondPolicyValidation:
    """Validator must enforce the closed vocabulary for ``respond_policy``.

    The policy field is per-recipient; the orchestrator filters
    ``RespondNever`` upstream of dispatch, so a ``never`` reaching the
    receiver is malformed routing. An empty string is also malformed —
    the gate requires the field and would otherwise have to fail-closed
    on a missing dimension.
    """

    def test_accepts_when_mentioned(self):
        err, _ = validate_channel_message_event(_event(respond_policy="when_mentioned"))
        assert err is None

    def test_accepts_always(self):
        err, _ = validate_channel_message_event(_event(respond_policy="always"))
        assert err is None

    def test_rejects_never(self):
        # ``never`` MUST NOT reach the receiver — orchestrator filters
        # it upstream. If it shows up, treat as malformed.
        err, _ = validate_channel_message_event(_event(respond_policy="never"))
        assert err is not None
        assert "respond_policy" in err

    def test_rejects_empty(self):
        err, _ = validate_channel_message_event(_event(respond_policy=""))
        assert err is not None
        assert "respond_policy" in err

    def test_rejects_unknown(self):
        err, _ = validate_channel_message_event(_event(respond_policy="weekly"))
        assert err is not None
        # Must not leak full attacker-controlled field on the wire.
        assert "weekly" in err  # still present (short token)
        assert len(err) < 200


class TestThreadParentSenderIDValidation:
    """``thread_parent_sender_id`` is optional; when set it must be a valid id."""

    def test_accepts_empty(self):
        # Empty is the proto-3 default for non-thread events.
        err, _ = validate_channel_message_event(_event(thread_parent_sender_id=""))
        assert err is None

    def test_accepts_valid_participant_id(self):
        err, _ = validate_channel_message_event(_event(thread_parent_sender_id="iron-fox"))
        assert err is None

    def test_rejects_invalid_participant_id(self):
        # Reserved colon — same trust boundary as ``sender_id``.
        err, _ = validate_channel_message_event(_event(thread_parent_sender_id="bad:id"))
        assert err is not None
        assert "thread_parent_sender_id" in err
