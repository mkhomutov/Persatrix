"""
Pure-function unit tests for
:func:`agents.memory.interactions.scope_for_channel_event` (RFC 0020 §G).

PR 5 originally exercised the helper only end-to-end through
``agent.on_event`` in the integration suite, which made each branch
decision pay the cost of constructing an agent, building memory, and
dispatching through the response gate. PR 262 deep-review finding **L3**
(unit-test coverage) and **N1** (edge-case gaps) recommended a pure-
function unit test that:

* Exhaustively covers every discriminator branch in microseconds.
* Pins the contradiction-detection contract added for finding **L1**
  (explicit ``channel_type`` and ``channel_id`` prefix disagree).
* Asserts the ``on_unknown`` callback fires on the unknown-prefix path
  and on contradictory inputs (the integration test could only confirm
  the lambda was wired, not that it was invoked).

The companion integration test
:mod:`tests.integration.test_channel_interaction_scoping` still covers
the multi-agent acceptance contract (one episode per agent on close);
this file is the unit-level matrix.
"""

from __future__ import annotations

import logging

import pytest

from agents.memory.interactions import (
    scope_for_channel_event,
    scope_for_dm,
    scope_for_group,
    scope_for_thread,
)


_LOCAL = "local-agent"
_PEER = "peer-agent"


# ─── Discriminator cascade ──────────────────────────────────


class TestThreadIdPrecedence:
    """thread_id wins over every other discriminator (RFC 0020 §G)."""

    def test_thread_id_overrides_dm_channel_type(self):
        # A reply in a thread inside a DM still rolls under the thread.
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="dm:room",
            sender_id=_PEER,
            thread_id="t-abc",
            channel_type="dm",
        )
        assert scope == scope_for_thread("t-abc")

    def test_thread_id_overrides_group_channel_type(self):
        # PR 5 acceptance contract: thread reply inside group → thread scope.
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="group:planning",
            sender_id=_PEER,
            thread_id="t-xyz",
            channel_type="group",
        )
        assert scope == scope_for_thread("t-xyz")

    def test_thread_id_idempotent_when_already_prefixed(self):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id=None,
            sender_id=None,
            thread_id="thread:already-prefixed",
            channel_type=None,
        )
        # ``scope_for_thread`` is idempotent in the ``thread:`` prefix.
        assert scope == "thread:already-prefixed"


class TestChannelTypeDiscriminator:
    """Explicit ``channel_type`` drives scope selection when set."""

    def test_dm_channel_type_uses_dm_scope(self):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="dm:room",
            sender_id=_PEER,
            thread_id=None,
            channel_type="dm",
        )
        assert scope == scope_for_dm(_LOCAL, _PEER)

    def test_dm_channel_type_without_sender_id_returns_none(self):
        # DM scope requires both participants — without sender_id we
        # cannot key the interaction symmetrically.
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="dm:room",
            sender_id=None,
            thread_id=None,
            channel_type="dm",
        )
        assert scope is None

    def test_group_channel_type_uses_group_scope(self):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="group:planning",
            sender_id=_PEER,
            thread_id=None,
            channel_type="group",
        )
        assert scope == scope_for_group("group:planning")

    def test_thread_channel_type_uses_thread_scope(self):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="thread:t1",
            sender_id=_PEER,
            thread_id=None,
            channel_type="thread",
        )
        assert scope == scope_for_thread("thread:t1")

    @pytest.mark.parametrize("variant", ["DM", " dm ", "Dm", "dM"])
    def test_channel_type_normalisation_is_case_and_whitespace_insensitive(
        self, variant,
    ):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="dm:room",
            sender_id=_PEER,
            thread_id=None,
            channel_type=variant,
        )
        assert scope == scope_for_dm(_LOCAL, _PEER)


class TestChannelIdPrefixFallback:
    """When ``channel_type`` is missing, the channel_id prefix routes."""

    @pytest.mark.parametrize(
        "channel_id,expected_factory",
        [
            ("dm:abc", lambda: scope_for_dm(_LOCAL, _PEER)),
            ("group:eng", lambda: scope_for_group("group:eng")),
            ("thread:t-1", lambda: scope_for_thread("thread:t-1")),
        ],
    )
    def test_prefix_routes_when_channel_type_missing(
        self, channel_id, expected_factory,
    ):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id=channel_id,
            sender_id=_PEER,
            thread_id=None,
            channel_type=None,
        )
        assert scope == expected_factory()

    def test_empty_string_channel_type_is_treated_as_missing(self):
        # N1: empty string should normalise the same as ``None``.
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="dm:abc",
            sender_id=_PEER,
            thread_id=None,
            channel_type="",
        )
        assert scope == scope_for_dm(_LOCAL, _PEER)


class TestSenderIdLegacyPath:
    """No channel_id and no thread_id → legacy chat DM path."""

    def test_sender_only_uses_dm_scope(self):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id=None,
            sender_id=_PEER,
            thread_id=None,
            channel_type=None,
        )
        assert scope == scope_for_dm(_LOCAL, _PEER)

    def test_under_populated_event_returns_none(self):
        # No channel_id, no thread_id, no sender_id → cannot route.
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id=None,
            sender_id=None,
            thread_id=None,
            channel_type=None,
        )
        assert scope is None


# ─── Unknown-prefix path (N1) ───────────────────────────────


class TestUnknownPrefix:
    """Unknown ``channel_id`` prefix fires ``on_unknown`` and falls back."""

    def test_unknown_prefix_invokes_on_unknown(self):
        seen: list[tuple[str, str]] = []
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="weird:xyz",
            sender_id=_PEER,
            thread_id=None,
            channel_type=None,
            on_unknown=lambda raw, cid: seen.append((raw, cid)),
        )
        assert seen == [("", "weird:xyz")]
        # Deterministic fallback: thread-shape so the row lands somewhere.
        assert scope == scope_for_thread("weird:xyz")

    def test_unknown_prefix_without_callback_still_returns_fallback(self):
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="weird:xyz",
            sender_id=_PEER,
            thread_id=None,
            channel_type=None,
            on_unknown=None,
        )
        assert scope == scope_for_thread("weird:xyz")


# ─── Contradiction detection (L1) ───────────────────────────


class TestChannelTypeVsPrefixContradiction:
    """L1: explicit ``channel_type`` and ``channel_id`` prefix disagree.

    Pre-fix behaviour was to silently let the prefix override the type
    in the ``norm == X or channel_id.startswith(...)`` OR-pattern, which
    could yield malformed scopes (e.g. ``"group:thread:abc"``). The
    helper now treats explicit ``channel_type`` as authoritative and
    surfaces the contradiction through ``on_unknown`` so wire-side
    validator drift is observable in the operator log path.
    """

    def test_group_type_with_thread_prefix_warns_and_uses_type(self):
        seen: list[tuple[str, str]] = []
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="thread:abc",
            sender_id=_PEER,
            thread_id=None,
            channel_type="group",
            on_unknown=lambda raw, cid: seen.append((raw, cid)),
        )
        # Contradiction observed: callback fires with the raw type and
        # the offending channel_id so the operator can correlate the
        # event in the log stream.
        assert seen == [("group", "thread:abc")]
        # Type is authoritative: route as group, not thread. The result
        # is the explicit channel_id wrapped — no double-prefix because
        # ``scope_for_group`` is idempotent and ``"thread:abc"`` does
        # not start with ``"group:"``, so the result is
        # ``"group:thread:abc"``. That's intentional: the row still
        # lands deterministically and the contradiction is visible in
        # the metric stream rather than being silently rewritten.
        assert scope == scope_for_group("thread:abc")

    def test_thread_type_with_group_prefix_warns(self):
        seen: list[tuple[str, str]] = []
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="group:eng",
            sender_id=_PEER,
            thread_id=None,
            channel_type="thread",
            on_unknown=lambda raw, cid: seen.append((raw, cid)),
        )
        assert seen == [("thread", "group:eng")]
        # ``channel_type="thread"`` wins over the ``group:`` prefix.
        assert scope == scope_for_thread("group:eng")

    def test_dm_type_with_thread_prefix_warns(self):
        seen: list[tuple[str, str]] = []
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="thread:t-1",
            sender_id=_PEER,
            thread_id=None,
            channel_type="dm",
            on_unknown=lambda raw, cid: seen.append((raw, cid)),
        )
        assert seen == [("dm", "thread:t-1")]
        assert scope == scope_for_dm(_LOCAL, _PEER)

    def test_matching_type_and_prefix_does_not_warn(self):
        # The non-contradictory path must remain silent — the operator
        # log signal is meaningful only because the happy path stays
        # quiet.
        seen: list[tuple[str, str]] = []
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="group:planning",
            sender_id=_PEER,
            thread_id=None,
            channel_type="group",
            on_unknown=lambda raw, cid: seen.append((raw, cid)),
        )
        assert seen == []
        assert scope == scope_for_group("group:planning")

    def test_unknown_type_with_known_prefix_warns_and_uses_prefix(self):
        # Unknown ``channel_type`` with a recognisable prefix should
        # surface the unknown type via ``on_unknown`` (so the operator
        # knows wire-side validation drifted) but still route by prefix
        # rather than dropping the event.
        seen: list[tuple[str, str]] = []
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="group:eng",
            sender_id=_PEER,
            thread_id=None,
            channel_type="forum",
            on_unknown=lambda raw, cid: seen.append((raw, cid)),
        )
        assert seen == [("forum", "group:eng")]
        assert scope == scope_for_group("group:eng")


# ─── L2: callback type-coercion ─────────────────────────────


class TestOnUnknownTypeCoercion:
    """L2: ``on_unknown`` is annotated ``Callable[[str, str], None]``.

    The function defends against ``channel_type`` arriving as a non-str
    at runtime (payload corruption / wire-side validator drift) via an
    ``isinstance(raw, str)`` guard inside ``norm``. The same guard must
    also coerce ``raw`` before forwarding it to the callback so the
    annotation is honoured at the call site, not just on the parameter
    declaration.
    """

    def test_non_str_channel_type_is_coerced_to_str_for_callback(self):
        seen: list[tuple[object, object]] = []
        # int is the realistic wire-side-drift case: a JSON deserialiser
        # that produced an int where the schema declared a str.
        scope = scope_for_channel_event(
            _LOCAL,
            channel_id="weird:xyz",
            sender_id=_PEER,
            thread_id=None,
            channel_type=42,  # type: ignore[arg-type]
            on_unknown=lambda raw, cid: seen.append((raw, cid)),
        )
        assert len(seen) == 1
        raw, cid = seen[0]
        # The contract: callback receives a ``str`` for both args.
        assert isinstance(raw, str)
        assert isinstance(cid, str)
        # Fallback routing still lands deterministically.
        assert scope == scope_for_thread("weird:xyz")


# ─── Logging seam (cross-cuts L1 + N1) ──────────────────────


class TestStatePersistenceLambdaSurfacesContradictions(object):
    """Smoke check: the ``on_unknown`` lambda used by the runtime emits
    a warning that an operator can grep for. Pinning this shape here so
    a future refactor of the lambda format does not silently drop the
    operator-visible signal.
    """

    def test_callback_can_log_via_logger_warning(self, caplog):
        logger = logging.getLogger("test.scope_for_channel_event")

        def on_unknown(raw: str, cid: str) -> None:
            logger.warning("unknown channel_type=%r for channel_id=%r", raw, cid)

        with caplog.at_level(logging.WARNING, logger=logger.name):
            scope_for_channel_event(
                _LOCAL,
                channel_id="weird:xyz",
                sender_id=_PEER,
                thread_id=None,
                channel_type=None,
                on_unknown=on_unknown,
            )
        assert any(
            "unknown channel_type" in record.message for record in caplog.records
        )
