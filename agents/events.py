"""Typed turn-event vocabulary — closed error taxonomies (RFC 0041 Phase 1).

[RFC 0041](../docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md)
introduces one ordered, typed event stream per agent turn.  This module is the
Phase-1 **PR 1** slice: the two closed error enums only.  The ``TurnEvent``
dataclass taxonomy (``ModelOutput`` / ``ToolCallEvent`` / ``ToolResultEvent`` /
``StateDelta`` / ``Error`` / ``Control`` / ``CallbackModelOutput``) lands in
PR 2 on top of these; see [the PR plan](../docs/rfcs/0041-pr-plan.md).

Leaf module — imports nothing from the agent runtime, so the event vocabulary
can be imported by the loop, the tool registry, and the eval harness without
creating a cycle.

**The taxonomies are closed** (RFC 0041 Goal 5): adding a member is an
RFC-level change, not a casual addition.  ``tests/unit/python/
test_events_enums.py`` asserts exact membership so a silent addition fails CI.

**Two layers, one denial.** A ``before_tool`` veto (RFC 0041 §D) surfaces at
both layers of the stream, and the two spellings are intentional:

* the **tool-result layer** emits ``ToolResultEvent(ok=False,
  error_kind=ToolErrorKind.DENIED)`` — the refusal the model sees on its next
  round, in the vocabulary of *a tool that did not run*; and
* the **turn layer** emits ``Error(kind=ErrorKind.TOOL_DENIED)`` — the same
  refusal in the vocabulary of *what went wrong this turn*, which is what the
  channel-publish site, the dead-letter subscriber, and the RFC 0044 goldens
  route on.

Collapsing them would force one consumer to re-derive the other's meaning,
which is precisely the ad-hoc pattern-matching RFC 0041 §M-1 exists to remove.

**Python-only in Phase 1.** No Go consumer routes on these values today — the
typed chat-error for ISSUE-0065 / ISSUE-0066 is published Python-side by
:mod:`agents.channel_publisher`.  If a Phase-2 Go subscriber ever routes on
``kind``, the enum must cross the wire under the repo's generated
cross-language parity discipline (``cmd/genpatterns`` →
:mod:`agents.security_enums`, gated by ``tests/unit/python/
test_pattern_parity.py``) or become a proto enum on ``proto/task.proto`` —
never a hand-copied string set.  See RFC 0041 §Security and Open Q #4.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ErrorKind",
    "ToolErrorKind",
]


class ErrorKind(StrEnum):
    """Turn-level failure taxonomy (RFC 0041 §A) — closed set.

    The ``kind`` tag on an ``Error`` event.  Consumers route on this tag
    instead of pattern-matching free text at the publish site, which is the
    defect RFC 0041 §M-1 traces through ISSUE-0065 / ISSUE-0066.

    A :class:`~enum.StrEnum`, so a member compares and serializes as its value
    (``ErrorKind.TOOL_DENIED == "tool_denied"``) — the publish site and the
    RFC 0044 golden traces both compare against raw strings.
    """

    #: The wallet refused the pre-charge outright (ISSUE-0065).
    WALLET_DENIED = "wallet_denied"
    #: The agent already holds the maximum concurrent leases (ISSUE-0066).
    LEASE_CAP = "lease_cap"
    #: Provider or orchestrator rate limit (ISSUE-0066).
    RATE_LIMIT = "rate_limit"
    #: gRPC ``RESOURCE_EXHAUSTED`` from the provider path (ISSUE-0066).
    RESOURCE_EXHAUSTED = "resource_exhausted"
    #: A ``before_tool`` callback vetoed the call (§D); the tool-result layer
    #: spells the same denial :attr:`ToolErrorKind.DENIED`.
    TOOL_DENIED = "tool_denied"
    #: Unclassified failure, including a callback that raised (§D).
    INTERNAL = "internal"


class ToolErrorKind(StrEnum):
    """Tool-result failure taxonomy (RFC 0041 §A) — closed set.

    The ``error_kind`` tag on a ``ToolResultEvent`` whose ``ok`` is ``False``.
    Distinct from :class:`ErrorKind`: this names why *one tool call* failed,
    not why the turn failed.  A tool failure does not necessarily abort the
    turn — the model sees the failed result and may recover on its next round.
    """

    #: A ``before_tool`` callback vetoed execution (§D).  The turn layer
    #: spells the same denial :attr:`ErrorKind.TOOL_DENIED`.
    DENIED = "denied"
    #: The tool exceeded its execution budget.
    TIMEOUT = "timeout"
    #: The model named a tool the registry does not have.
    NOT_FOUND = "not_found"
    #: Arguments failed the tool's schema validation.
    INVALID_ARGS = "invalid_args"
    #: The tool raised an unclassified exception.
    INTERNAL = "internal"
