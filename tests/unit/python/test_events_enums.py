"""Closed error-enum tests (RFC 0041 Phase 1, PR 1).

[RFC 0041](../../../docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md)
§A declares :class:`~agents.events.ErrorKind` and
:class:`~agents.events.ToolErrorKind` **closed** (Goal 5: "adding a new event
kind is an RFC-level change, not a casual addition").  A closed set is only
closed if something fails when it silently grows, so the membership assertions
below are deliberately exact-match rather than ``in`` checks.

The value *strings* matter as much as the members: they are what lands in
``Error.kind`` / ``ToolResultEvent.error_kind``, what the channel-publish site
routes on, and what RFC 0044's ``event_sequence`` goldens assert against.
Renaming a value is a wire-visible change even while the enums stay
Python-only (RFC 0041 §A), so each value is pinned literally here.
"""

from __future__ import annotations

from enum import Enum

from agents.events import ErrorKind, ToolErrorKind


class TestErrorKindClosedSet:
    """``ErrorKind`` — the turn-level failure taxonomy (RFC 0041 §A)."""

    def test_membership_is_exactly_the_closed_set(self) -> None:
        assert {m.name for m in ErrorKind} == {
            "WALLET_DENIED",
            "LEASE_CAP",
            "RATE_LIMIT",
            "RESOURCE_EXHAUSTED",
            "TOOL_DENIED",
            "INTERNAL",
        }

    def test_values_are_pinned(self) -> None:
        assert {m.value for m in ErrorKind} == {
            "wallet_denied",
            "lease_cap",
            "rate_limit",
            "resource_exhausted",
            "tool_denied",
            "internal",
        }

    def test_covers_the_issue_0065_0066_failure_modes(self) -> None:
        # RFC 0041 §M-1: the error-reply incidents this taxonomy exists to type.
        assert ErrorKind.WALLET_DENIED.value == "wallet_denied"      # ISSUE-0065
        assert ErrorKind.LEASE_CAP.value == "lease_cap"              # ISSUE-0066
        assert ErrorKind.RATE_LIMIT.value == "rate_limit"            # ISSUE-0066
        assert ErrorKind.RESOURCE_EXHAUSTED.value == "resource_exhausted"


class TestToolErrorKindClosedSet:
    """``ToolErrorKind`` — the tool-result failure taxonomy (RFC 0041 §A)."""

    def test_membership_is_exactly_the_closed_set(self) -> None:
        assert {m.name for m in ToolErrorKind} == {
            "DENIED",
            "TIMEOUT",
            "NOT_FOUND",
            "INVALID_ARGS",
            "INTERNAL",
        }

    def test_values_are_pinned(self) -> None:
        assert {m.value for m in ToolErrorKind} == {
            "denied",
            "timeout",
            "not_found",
            "invalid_args",
            "internal",
        }


class TestStrEnumBehaviour:
    """Both enums are ``str, Enum`` so members compare/serialize as their value."""

    def test_members_are_str_instances(self) -> None:
        assert isinstance(ErrorKind.INTERNAL, str)
        assert isinstance(ToolErrorKind.DENIED, str)

    def test_members_compare_equal_to_their_value(self) -> None:
        # The publish site and the RFC 0044 goldens compare against raw strings.
        assert ErrorKind.TOOL_DENIED == "tool_denied"
        assert ToolErrorKind.DENIED == "denied"

    def test_lookup_by_value_round_trips(self) -> None:
        assert ErrorKind("wallet_denied") is ErrorKind.WALLET_DENIED
        assert ToolErrorKind("invalid_args") is ToolErrorKind.INVALID_ARGS

    def test_both_are_enums(self) -> None:
        assert issubclass(ErrorKind, Enum)
        assert issubclass(ToolErrorKind, Enum)


class TestTwoLayerDenialMapping:
    """RFC 0041 §D — one denial, spelled at two layers.

    A ``before_tool`` veto emits ``Error(kind=ErrorKind.TOOL_DENIED)`` at the
    turn layer *and* a synthetic ``ToolResultEvent(error_kind=
    ToolErrorKind.DENIED)`` at the tool-result layer.  The two spellings are
    intentional, not a duplication — this test pins that intent so a future
    "cleanup" that collapses them fails loudly.
    """

    def test_the_two_denial_spellings_are_distinct(self) -> None:
        assert ToolErrorKind.DENIED.value != ErrorKind.TOOL_DENIED.value
        assert ToolErrorKind.DENIED.value == "denied"
        assert ErrorKind.TOOL_DENIED.value == "tool_denied"

    def test_tool_denied_is_the_turn_layer_counterpart(self) -> None:
        # The turn-layer value is the tool-layer value, namespaced by "tool_".
        assert ErrorKind.TOOL_DENIED.value == f"tool_{ToolErrorKind.DENIED.value}"

    def test_internal_exists_on_both_layers_independently(self) -> None:
        # A callback raising (§D) is ErrorKind.INTERNAL; a tool raising is
        # ToolErrorKind.INTERNAL.  Same spelling, different taxonomies.
        assert ErrorKind.INTERNAL.value == ToolErrorKind.INTERNAL.value == "internal"
        assert ErrorKind.INTERNAL is not ToolErrorKind.INTERNAL
