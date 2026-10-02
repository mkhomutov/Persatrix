"""Typed turn-event vocabulary — closed error taxonomies (RFC 0041 Phase 1).

[RFC 0041](../docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md)
introduces one ordered, typed event stream per agent turn.  This module is the
Phase-1 vocabulary: the two closed error enums (PR 1) and the ``TurnEvent``
dataclass taxonomy — ``ModelOutput`` / ``ToolCallEvent`` / ``ToolResultEvent``
/ ``StateDelta`` / ``Error`` / ``Control`` / ``CallbackModelOutput`` (PR 2).
The callback Protocol and the bounded stream follow in PRs 3–4; see
[the PR plan](../docs/rfcs/0041-pr-plan.md).

Near-leaf module — its only project import is :mod:`agents.llm_types` (itself
a leaf that imports nothing project-internal), reusing ``StopReason`` and
``Usage`` rather than restating them.  Nothing here imports the agent loop, so
the loop, the tool registry, and the eval harness can all import this
vocabulary without creating a cycle.

**The taxonomies are closed** (RFC 0041 Goal 5): adding an enum member or an
event type is an RFC-level change, not a casual addition.
``tests/unit/python/test_events_enums.py`` and ``…/test_events_taxonomy.py``
assert exact membership so a silent addition fails CI.

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

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from .llm_types import StopReason, Usage

__all__ = [
    "CallbackModelOutput",
    "Control",
    "Error",
    "ErrorKind",
    "ModelOutput",
    "StateDelta",
    "ToolCallEvent",
    "ToolErrorKind",
    "ToolResultEvent",
    "TurnEvent",
    "new_event_id",
]


def new_event_id() -> str:
    """Mint an opaque unique id for a :class:`TurnEvent`.

    RFC 0041 §A sketches ``event_id`` / ``turn_id`` as ULIDs for sortability.
    This returns a ``uuid4().hex`` token instead, following the precedent the
    repo already set for the RFC 0020 ``interaction_id`` — "an opaque uuid4
    token, not a ULID despite RFC 0020 §D's wording" (see
    :mod:`agents.channel_wire_metadata`).  Adding a ULID dependency to buy
    sortability would be redundant here: **ordering within a turn is carried
    by** :attr:`TurnEvent.seq`, not by id sortability (§B).  ``event_id`` is
    the *reference* key — what :attr:`Error.cause_event_id` and the §B
    redaction transform address events by — and uniqueness is all that needs.
    """
    return uuid.uuid4().hex


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


# ─── Event taxonomy (RFC 0041 §A) ──────────────────────────────────────
#
# Every event is a frozen dataclass: the stream is an audit record, and a
# subscriber must not be able to rewrite what another subscriber sees.
#
# Note on hashing: ``frozen=True`` generates ``__hash__``, but events whose
# payload carries a dict (``ToolCallEvent.args``, ``StateDelta.value``) or a
# non-frozen ``Usage`` are not hashable in practice.  Nothing in the design
# hashes an event — identity is :attr:`TurnEvent.event_id`.


@dataclass(frozen=True)
class TurnEvent:
    """Base of the closed event taxonomy — the four fields every event carries.

    ``(turn_id, seq)`` is the **ordering** key within a turn; ``event_id`` is
    the **reference** key (§B).  Subclasses add their own payload; adding a new
    subclass is an RFC-level change (Goal 5).
    """

    #: Opaque unique id — see :func:`new_event_id`.
    event_id: str
    #: The turn this event belongs to; stable across a turn's model-call retries.
    turn_id: str
    #: 0-indexed position within the turn, monotonically increasing.
    seq: int
    #: Timezone-aware UTC timestamp of emission.
    occurred_at: datetime


@dataclass(frozen=True)
class ModelOutput(TurnEvent):
    """The assistant's model output for one model call.

    Channel publish subscribes to *this* type and ignores
    :class:`CallbackModelOutput` (§E), so a callback's own model call is never
    mistaken for the assistant's turn output.

    ``stop_reason`` reuses the runtime :class:`~agents.llm_types.StopReason`
    enum.  The RFC's original sketch added an ``"error"`` literal; it is
    deliberately absent — a failure is a separate :class:`Error` event, not a
    stop reason.
    """

    role: Literal["assistant"]
    content: str
    stop_reason: StopReason
    token_usage: Usage


@dataclass(frozen=True)
class ToolCallEvent(TurnEvent):
    """A tool invocation the model requested.

    Named ``ToolCallEvent``, **not** ``ToolCall``: the runtime already defines
    :class:`agents.llm_types.ToolCall` (imported by :mod:`agents.base`).
    """

    tool_name: str
    args: dict[str, Any]
    #: Correlates with the :attr:`ToolResultEvent.tool_call_id` that answers it.
    tool_call_id: str


@dataclass(frozen=True)
class ToolResultEvent(TurnEvent):
    """The outcome of a tool invocation.

    Named ``ToolResultEvent``, **not** ``ToolResult``: the runtime already
    defines ``ToolResult`` in :mod:`agents.tools.registry` — the very module
    that emits this event.

    A failure carries :attr:`error_kind`; a ``before_tool`` veto (§D) yields a
    synthetic instance with ``ok=False`` and
    :attr:`ToolErrorKind.DENIED`, whose turn-layer counterpart is
    ``Error(kind=ErrorKind.TOOL_DENIED)``.
    """

    tool_call_id: str
    ok: bool
    content: str | dict[str, Any]
    error_kind: ToolErrorKind | None = None


@dataclass(frozen=True)
class StateDelta(TurnEvent):
    """A state *write* (not a recall read).

    ``scope`` is an opaque :class:`str` in Phase 1 so this RFC and RFC 0042 can
    land independently; RFC 0042 narrows it to the closed ``Scope`` set
    (``app | persona | channel | session | interaction | temp``) in the Phase-2
    sweep.  Subscribers must accept any string value until then.
    """

    scope: str
    key: str
    op: Literal["set", "delete", "increment"]
    value: Any | None = None


@dataclass(frozen=True)
class Error(TurnEvent):
    """A typed failure.

    An *event*, not an exception — it is emitted onto the stream, never
    raised.  :attr:`kind` is the closed tag consumers route on instead of
    pattern-matching free text at the publish site (§M-1).
    """

    kind: ErrorKind
    message: str
    retryable: bool
    #: The :attr:`TurnEvent.event_id` of the event that caused this failure.
    cause_event_id: str | None = None


@dataclass(frozen=True)
class Control(TurnEvent):
    """Turn lifecycle boundary.

    Every turn opens with ``turn_started`` and closes with exactly one of
    ``turn_completed`` / ``turn_aborted`` (§B).  An aborting turn emits its
    terminal :class:`Error` *before* the ``turn_aborted`` event.
    """

    kind: Literal["turn_started", "turn_completed", "turn_aborted"]
    reason: str | None = None


@dataclass(frozen=True)
class CallbackModelOutput(TurnEvent):
    """A *callback's own* model call — e.g. a moderation LLM (OQ #3).

    Deliberately a distinct type rather than ``ModelOutput(role="callback")``:
    channel publish subscribes to :class:`ModelOutput` only (§E), so reusing
    that type here would publish a moderation call as the assistant's turn
    output.  Token attribution reads :attr:`token_usage` off this type so
    in-callback spend is accounted separately from the turn's own.
    """

    callback_name: str
    content: str
    token_usage: Usage
