"""The decision contract's types (RFC 0028 §A and §D; PR plan PR 1, E-5).

An agent keeps choosing what to do next — reply, call a tool, stay silent —
and today nothing writes down which choice it made or why. These types are
the shape of that written record, a ``DecisionRecord``. Nothing here decides
anything or runs in a turn yet: the selection pipeline (PR 2), the decision
log (PR 3) and the checkpoints in the two runtimes (PRs 5 and 6) build on
them.

Every type is frozen. A record turns into one JSON line and back unchanged,
because that line is what the decision log stores and what the replay
harness (PR 8) reads.

A record holds no text from the turn (E-10). A candidate's message, reply or
tool-argument values appear only as ``content_hash``, a SHA-256 digest; the
runtime keeps the action itself and finds it again by ``candidate_id``.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import math
import re
import time
import typing
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import UnionType
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "RECORD_FORMAT_VERSION",
    "ActionClass",
    "Checkpoint",
    "DecisionCandidate",
    "DecisionContext",
    "DecisionOutcome",
    "DecisionPolicy",
    "DecisionRecord",
    "DecisionSubject",
    "DirectiveVerdict",
    "PolicyMode",
    "Rejection",
    "hash_arguments",
    "hash_content",
]

#: The ``"v"`` of a record's JSON line. A field added with a default keeps
#: it, since older lines read with the default. It goes up only for a change
#: older lines could not be read under, and that change keeps reading them:
#: a reader takes its own version and every older one, and refuses newer.
RECORD_FORMAT_VERSION = 1

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]*")


class ActionClass(Enum):
    """The kind of next action a pre-act checkpoint chooses (§A)."""

    RESPOND = "respond"
    ASK_CLARIFICATION = "ask_clarification"
    TOOL_CALL = "tool_call"
    DELEGATE = "delegate"
    PUBLISH_CHANNEL = "publish_channel"
    DEFER = "defer"


class Checkpoint(Enum):
    """Where in a turn a decision is taken (§A)."""

    PRE_ACT = "pre_act"
    PRE_TOOL = "pre_tool"
    PRE_DELEGATE = "pre_delegate"
    POST_OUTCOME = "post_outcome"


class DecisionSubject(Enum):
    """What a record decides: the agent's next action, or a directive (E-5)."""

    ACTION = "action"
    DIRECTIVE = "directive"


class DirectiveVerdict(Enum):
    """RFC 0012 §F's answer to a directive. A tie gets none: it defers."""

    ACCEPT = "accept"
    ADAPT = "adapt"
    REFUSE = "refuse"


class PolicyMode(Enum):
    """Who chooses among the permitted candidates (§B)."""

    MANUAL = "manual"
    SEMI_AUTOMATED = "semi_automated"
    AUTOMATED = "automated"


def hash_content(text: str) -> str:
    """The digest a candidate carries in place of its text (E-10).

    ``surrogatepass`` lets a reply cut mid-emoji, which can hold a lone
    surrogate, be hashed instead of raising.
    """
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def hash_arguments(arguments: Mapping[str, Any]) -> str:
    """The digest of a tool call's argument values, the same in any key order,
    so both runtimes give one call one hash."""
    return hash_content(json.dumps(arguments, sort_keys=True, separators=(",", ":")))


def _as_float(value: float, name: str) -> float:
    """A finite number as a float, so the JSON line has one spelling for it."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} is too large for a float") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _check_reason(reason: str | None) -> None:
    if reason is not None and not _REASON_CODE.fullmatch(reason):
        raise ValueError("a reason must be a short code such as no_candidate, never text")


@dataclass(frozen=True)
class DecisionContext:
    """What the checkpoint knew when it decided: labels and ids, no text.

    The optional fields carry a persona's state (§D); a task agent leaves
    them empty.
    """

    checkpoint: Checkpoint
    agent_id: str
    #: Tells a manual record from a scored one, and semi-automated from
    #: automated, so calibration can pick the records it needs.
    mode: PolicyMode
    agent_type: str | None = None
    #: An ``EventType`` value, or ``"workflow_step"`` for a task.
    event_type: str | None = None
    #: The inbound message id or task id the turn answers.
    event_id: str | None = None
    workflow_id: str | None = None
    #: The acting channel and its RFC 0037 protection level.
    channel_id: str | None = None
    protection_level: str | None = None
    autonomy_level: str | None = None
    #: A ``Mood`` value.
    mood: str | None = None


@dataclass(frozen=True)
class DecisionCandidate:
    """One action the agent could take."""

    #: The runtime's handle for the action; unique within a record.
    candidate_id: str
    action_class: ActionClass
    #: The runtime's own name for it: an ``ActionType`` value for a persona,
    #: a ``StopReason`` value for a task-agent step.
    action: str
    tool_name: str | None = None
    argument_names: tuple[str, ...] = ()
    #: The channel or agent the action addresses.
    target: str | None = None
    #: ``hash_content`` of the message or reply, or ``hash_arguments`` of the
    #: argument values.
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.content_hash is not None and not _SHA256_HEX.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a SHA-256 hex digest, never the text")


@dataclass(frozen=True)
class Rejection:
    """A candidate a constraint or gate removed, and which one did it."""

    candidate_id: str
    #: A short code, like the outcome's.
    reason: str

    def __post_init__(self) -> None:
        _check_reason(self.reason)


@dataclass(frozen=True)
class DecisionOutcome:
    """What a policy chose (§D)."""

    #: The selected candidate's class; ``DEFER`` when nothing is selected.
    #: ``None`` only on a directive record that reached a verdict: the
    #: directive's own row says which class it governs, when it says one.
    action_class: ActionClass | None
    selected_id: str | None = None
    #: A short code such as ``no_candidate`` or ``approval_required``, never
    #: text from the turn (E-10).
    reason: str | None = None
    #: In [0, 1]; ``None`` in manual mode, which does not score.
    confidence: float | None = None
    rejected: tuple[Rejection, ...] = ()
    #: Written by the policy from labels; never quotes a candidate.
    rationale: str = ""
    verdict: DirectiveVerdict | None = None

    def __post_init__(self) -> None:
        _check_reason(self.reason)
        if self.confidence is not None:
            object.__setattr__(self, "confidence", _as_float(self.confidence, "confidence"))
            if not 0.0 <= self.confidence <= 1.0:
                raise ValueError(f"confidence {self.confidence!r} is outside [0, 1]")


@dataclass(frozen=True)
class DecisionRecord:
    """One checkpoint's decision: what it saw, what it could do, what it chose."""

    context: DecisionContext
    candidates: tuple[DecisionCandidate, ...]
    outcome: DecisionOutcome
    subject: DecisionSubject = DecisionSubject.ACTION
    #: The directive's id when ``subject`` is ``DIRECTIVE``.
    subject_id: str | None = None
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: Real time: ``agents.clock`` keeps telemetry and whatever reaches the
    #: orchestrator on it, as the record's span and an approval's expiry are.
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recorded_at", _as_float(self.recorded_at, "recorded_at"))
        _check_record(self)

    def to_json(self) -> str:
        """The record as one JSON line, keys sorted, without a newline."""
        data = {"v": RECORD_FORMAT_VERSION, **_encode(self)}
        return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, line: str) -> DecisionRecord:
        """Read a line ``to_json`` wrote; raise ``ValueError`` on anything else."""
        try:
            data = json.loads(line)
        except RecursionError as exc:
            raise ValueError("a decision record line is nested too deeply") from exc
        version = data.pop("v", None) if isinstance(data, dict) else None
        if type(version) is not int or not 1 <= version <= RECORD_FORMAT_VERSION:
            raise ValueError(f"not a decision record of versions 1 to {RECORD_FORMAT_VERSION}")
        # Their defaults would give the line a new id, or the reader's clock.
        if missing := {"record_id", "recorded_at"} - data.keys():
            raise ValueError(f"a decision record line lacks {sorted(missing)}")
        record: DecisionRecord = _decode(cls, data)
        return record


@runtime_checkable
class DecisionPolicy(Protocol):
    """A strategy that picks among candidates (§D). Manual mode's is PR 2's."""

    def select(
        self, context: DecisionContext, candidates: Sequence[DecisionCandidate]
    ) -> DecisionOutcome: ...


def _check_record(record: DecisionRecord) -> None:
    outcome = record.outcome
    classes = {c.candidate_id: c.action_class for c in record.candidates}
    if len(classes) != len(record.candidates):
        raise ValueError("candidate ids must be unique")
    rejected = [r.candidate_id for r in outcome.rejected]
    if len(set(rejected)) != len(rejected) or not set(rejected) <= classes.keys():
        raise ValueError("each rejection must name a different candidate")
    if outcome.selected_id is not None:
        if outcome.selected_id in rejected:
            raise ValueError("the selected candidate cannot also be rejected")
        if (outcome.selected_id not in classes
                or classes[outcome.selected_id] is not outcome.action_class):
            raise ValueError("the selection must name a candidate of the outcome's class")
    deferred = outcome.action_class is ActionClass.DEFER and bool(outcome.reason)
    if record.subject is DecisionSubject.ACTION:
        if outcome.action_class is None or outcome.verdict is not None:
            raise ValueError("an action record has an action class and no verdict")
        if record.subject_id is not None:
            raise ValueError("only a directive record names a subject id")
        if outcome.selected_id is None and not deferred:
            raise ValueError("with nothing selected the outcome must defer, with a reason")
    else:
        if not record.subject_id:
            raise ValueError("a directive record must name its directive")
        reached = outcome.verdict is not None and outcome.action_class is None
        if not reached and not (deferred and outcome.verdict is None):
            raise ValueError(
                "a directive gets a verdict and no action class, or defers with a reason"
            )


# ─── The JSON line ─────────────────────────────────────────
# Encoding walks the dataclasses; decoding walks their type hints. A field
# added later with a default is read from older lines, which lack it. The
# decoder reads str, float, the enums and dataclasses here, optional values
# and ``tuple[X, ...]`` of those; a field of any other type (an int, a bool,
# a mapping, a fixed-length tuple) needs its own branch and test first.


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value


def _decode(hint: Any, raw: Any) -> Any:
    origin = typing.get_origin(hint)
    if origin in (typing.Union, UnionType):
        if raw is None and type(None) in typing.get_args(hint):
            return None
        (inner,) = [a for a in typing.get_args(hint) if a is not type(None)]
        return _decode(inner, raw)
    if origin is tuple:
        if not isinstance(raw, list):
            raise ValueError(f"expected a list, got {raw!r}")
        args = typing.get_args(hint)
        if args[1:] != (Ellipsis,):
            raise ValueError(f"the decoder reads tuple[X, ...] only, not {hint!r}")
        return tuple(_decode(args[0], item) for item in raw)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(raw)
    if dataclasses.is_dataclass(hint):
        return _decode_dataclass(hint, raw)
    if hint is float:
        return _as_float(raw, "a number field")
    if hint is str and isinstance(raw, str):
        return raw
    raise ValueError(f"expected {hint!r}, got {raw!r}")


@functools.cache
def _hints(cls: type) -> dict[str, Any]:
    """A dataclass's field types, worked out once per class, not per line."""
    return typing.get_type_hints(cls)


def _decode_dataclass(cls: Any, raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise ValueError(f"expected an object for {cls.__name__}, got {raw!r}")
    hints = _hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    if unknown := raw.keys() - names:
        raise ValueError(f"{cls.__name__} has no field {sorted(unknown)}")
    try:
        return cls(**{name: _decode(hints[name], value) for name, value in raw.items()})
    except TypeError as exc:  # a required field is missing
        raise ValueError(f"{cls.__name__}: {exc}") from exc
