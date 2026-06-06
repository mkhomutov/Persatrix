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

from typing import Any

import pytest

from agents.channel_validation import (
    _safe_repr,
    parse_channel_timestamp,
    validate_channel_message_dict,
    validate_channel_message_event,
)
from agents.generated import task_pb2


def _event(**overrides: object) -> task_pb2.ChannelMessageEvent:
    fields: dict[str, Any] = {
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


# ─── PR-265 review L1: dict-shape validation parity ─────────


def _msg_dict(**overrides: object) -> dict:
    """Build the JSON dict shape returned by
    ``GET /api/v1/channels/{id}/messages`` (see
    ``internal/server/channel_types.go::channelMessageResponse``).
    """
    fields: dict = {
        "id": "msg-001",
        "channel_id": "group:general",
        "sender_id": "iron-fox",
        "content": "hello",
        "timestamp": "2026-05-04T00:00:00Z",
        "thread_id": "",
        "mentions": [],
    }
    fields.update(overrides)
    return fields


class TestValidateChannelMessageDict:
    """``validate_channel_message_dict`` is the dict-shape sibling of
    ``validate_channel_message_event``. The catch-up fetcher
    (``agents.channel_catchup``) uses it to give the REST/JSON catch-up
    path the same defense-in-depth bounds the live gRPC path enforces
    via ``validate_channel_message_event``.

    Why a separate function instead of building a synthetic
    ``ChannelMessageEvent`` proto: the JSON shape lacks
    ``respond_policy`` (resolved from membership) and
    ``thread_parent_sender_id`` (not on the wire shape, see PR-265 L2).
    Building a partial proto just to re-use the existing validator
    would either silently default-fill those fields or require
    conditional asserts at the call site. The dict variant validates
    only the fields present on the JSON wire shape.

    Mirrors the same module-level constants as
    ``validate_channel_message_event`` so drift between the two paths
    is impossible by construction.
    """

    def test_accepts_well_formed_dict(self):
        err, ts = validate_channel_message_dict(
            _msg_dict(timestamp="2026-05-04T12:34:56Z"),
            channel_type="group",
        )
        assert err is None
        assert ts == pytest.approx(1777898096.0, abs=1.0)

    def test_rejects_oversize_content(self):
        err, ts = validate_channel_message_dict(
            _msg_dict(content="x" * 4001), channel_type="group",
        )
        assert err is not None
        assert "content" in err
        assert ts is None

    def test_rejects_too_many_mentions(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(mentions=[f"agent-{i:02d}" for i in range(11)]),
            channel_type="group",
        )
        assert err is not None
        assert "mentions" in err

    def test_rejects_invalid_mention(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(mentions=["bad:id"]), channel_type="group",
        )
        assert err is not None
        assert "mentions" in err

    def test_rejects_invalid_sender_id(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(sender_id="BAD-Sender"), channel_type="group",
        )
        assert err is not None
        assert "sender_id" in err

    def test_rejects_oversize_channel_id(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(channel_id="group:" + "x" * 256), channel_type="group",
        )
        assert err is not None
        assert "channel_id" in err

    def test_rejects_oversize_message_id(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(id="x" * 65), channel_type="group",
        )
        assert err is not None
        assert "message_id" in err

    def test_rejects_unknown_channel_type(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(), channel_type="weekly",
        )
        assert err is not None
        assert "channel_type" in err

    def test_rejects_channel_type_prefix_mismatch(self):
        # ``channel_type=group`` requires ``channel_id`` to start with
        # ``group:`` — see RFC 0011 §B.
        err, _ = validate_channel_message_dict(
            _msg_dict(channel_id="dm:a:b"), channel_type="group",
        )
        assert err is not None

    def test_rejects_naive_timestamp(self):
        # Same RFC 3339 contract as the live path: naive datetime
        # silently shifts by the host TZ offset. PR #248 nice-to-have.
        err, ts = validate_channel_message_dict(
            _msg_dict(timestamp="2026-05-04T00:00:00"),
            channel_type="group",
        )
        assert err is not None
        assert "timestamp" in err
        assert ts is None

    def test_rejects_oversize_thread_id(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(thread_id="x" * 129), channel_type="group",
        )
        assert err is not None
        assert "thread_id" in err

    def test_accepts_missing_optional_fields(self):
        # ``thread_id`` and ``mentions`` are optional on the JSON shape;
        # absence must not be conflated with empty-string violations.
        err, ts = validate_channel_message_dict(
            {
                "id": "msg-1",
                "channel_id": "group:general",
                "sender_id": "iron-fox",
                "content": "hi",
                "timestamp": "2026-05-04T00:00:00Z",
            },
            channel_type="group",
        )
        assert err is None
        assert isinstance(ts, float)

    def test_rejects_non_string_sender_id(self):
        # Wire-shape malformation: non-string types where strings are
        # required must reject rather than crash with TypeError.
        err, _ = validate_channel_message_dict(
            _msg_dict(sender_id=123), channel_type="group",
        )
        assert err is not None


class TestEveryoneBroadcastSentinel:
    """ISSUE-0094 (F-8): the ``@everyone`` broadcast sentinel
    (RFC 0030 relevance amendment Tier A, decision D3) must pass inbound
    envelope validation, not be rejected as a malformed participant id.

    The orchestrator (``internal/channels/channels.go``'s ``MentionEveryone``,
    the floor-control directed-filter bypass) and the receiver response gate
    (``agents.response_gate.MENTION_EVERYONE``) both special-case the sentinel;
    the inbound validators must agree, or a broadcast is dropped before the
    gate runs (every persona stays silent; a floor-controlled publish blocks
    N x 45s). See ``docs/issues/ISSUE-0094-…`` and the v0.3.7 execution report.
    """

    def test_event_accepts_everyone_alone(self):
        err, ts = validate_channel_message_event(_event(mentions=["@everyone"]))
        assert err is None
        assert isinstance(ts, float)

    def test_event_accepts_everyone_alongside_real_id(self):
        # The live repro shape: a broadcast that also names a specific
        # persona (`--mention-all` + an explicit @-mention).
        err, ts = validate_channel_message_event(
            _event(mentions=["ember-owl", "@everyone"]),
        )
        assert err is None
        assert isinstance(ts, float)

    def test_event_still_rejects_a_real_malformed_id_with_everyone_present(self):
        # The sentinel carve-out must not become a hole: a genuinely
        # malformed id alongside @everyone must still reject.
        err, _ = validate_channel_message_event(
            _event(mentions=["@everyone", "bad:id"]),
        )
        assert err is not None
        assert "mentions" in err

    def test_event_does_not_treat_everyone_as_the_sender(self):
        # The carve-out is scoped to the mentions list only — sender_id
        # carries a stronger trust claim and must still reject the sentinel.
        err, _ = validate_channel_message_event(_event(sender_id="@everyone"))
        assert err is not None
        assert "sender_id" in err

    def test_event_rejects_a_miscased_sentinel(self):
        # The carve-out is an exact-string match, deliberately mirroring Go's
        # ``const MentionEveryone = "@everyone"`` (channels.go) and the gate's
        # ``MENTION_EVERYONE`` — none of which case-fold. A miscased ``@Everyone``
        # is therefore NOT the sentinel: it fails the participant-id regex and
        # must reject, so the three layers can never silently diverge on casing.
        err, _ = validate_channel_message_event(_event(mentions=["@Everyone"]))
        assert err is not None
        assert "mentions" in err

    def test_dict_accepts_everyone_alone(self):
        err, ts = validate_channel_message_dict(
            _msg_dict(mentions=["@everyone"]), channel_type="group",
        )
        assert err is None
        assert isinstance(ts, float)

    def test_dict_accepts_everyone_alongside_real_id(self):
        err, ts = validate_channel_message_dict(
            _msg_dict(mentions=["ember-owl", "@everyone"]), channel_type="group",
        )
        assert err is None
        assert isinstance(ts, float)

    def test_dict_still_rejects_a_real_malformed_id_with_everyone_present(self):
        err, _ = validate_channel_message_dict(
            _msg_dict(mentions=["@everyone", "bad:id"]), channel_type="group",
        )
        assert err is not None
        assert "mentions" in err

    def test_sentinel_constant_matches_the_response_gate(self):
        # Drift guard: the validator pins the sentinel locally (hot-path
        # import-light, mirroring the locally-pinned participant-id regex),
        # so assert it stays equal to the canonical receiver-gate constant.
        from agents.channel_validation import _MENTION_EVERYONE
        from agents.response_gate import MENTION_EVERYONE

        assert _MENTION_EVERYONE == MENTION_EVERYONE == "@everyone"
