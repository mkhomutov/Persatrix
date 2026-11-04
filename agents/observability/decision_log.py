"""The decision log: one JSON line for each decision an agent records
(RFC 0028 §D; PR plan PR 3, E-10).

Each decision checkpoint (PRs 5 and 6) gives a ``DecisionRecord``. While
recording is on — the agent has a ``decision:`` block or belongs to an
organization (PR 4) — :class:`DecisionRecorder` sends it to two sinks:

* the agent's own file, ``<dir>/<agent id>.jsonl``, one line per record,
  appended in one write as :mod:`agents.call_log` does. It is the source of
  truth, which the replay harness (PR 8) reads. A line is the record's own
  JSON line with every string in it masked by :class:`SecretRedactor`
  first, so a record with nothing to mask is written as ``to_json`` gives it;
* one ``agent.decision.checkpoint`` span with the class chosen, the number
  of candidates and the confidence. Spans are sampled; nothing relies on
  them.

With recording off neither runs, no file or directory is made, and no
metric moves.

**What a line holds.** No text from the turn: a candidate's message, reply
or tool-argument values appear only as its SHA-256 ``content_hash`` (PR 1),
beside its class, ids, tool name and argument names, and the context's
acting channel and protection level. The hash is not keyed, so a short or
guessable text — a yes or no, a number, a channel name — can be found by
hashing guesses (PR 1 review F-14). That limit is recorded rather than
keyed: the file is readable by its owner only, the same owner whose memory
store holds the texts themselves, and a key would stop replay and the eval
harness matching a hash to a known text.

**Size.** When the next line would take the file past ``max_bytes``, the
file becomes ``.1`` (``.1`` becomes ``.2``, and so on up to ``keep``; the
oldest is dropped), and ``keep: 0`` drops the full file. A line longer than
the cap is still written, alone in a new file. The settings are the agent's
``decision.log`` keys: ``dir`` (default ``data/decisions``), ``max_bytes``
(10 MiB) and ``keep`` (5). One process writes an agent's file, so rotation
does not guard against a second.

**Failure.** A sink that fails — the disk, the redactor, the tracer — is
counted (``agent.decision.sink_errors``) and logged by the error's type,
never its message, which may quote the turn; the turn goes on.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentelemetry import trace

from ..decision.decision_types import DecisionRecord
from . import _metrics_decision
from .redact import Redactor, SecretRedactor
from .spans import DECISION_CHECKPOINT_SPAN

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

#: The canonical agent id, which is also a safe file name.
_AGENT_ID = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")
_LOG_KEYS = frozenset({"dir", "max_bytes", "keep"})


@dataclass(frozen=True)
class DecisionLogConfig:
    """An agent's ``decision.log`` settings."""

    dir: str = "data/decisions"
    max_bytes: int = 10 * 1024 * 1024
    #: How many rotated files are kept beside the one being written.
    keep: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.dir, str) or not self.dir:
            raise ValueError("decision.log.dir must be a path")
        if not _is_whole(self.max_bytes) or self.max_bytes < 1:
            raise ValueError("decision.log.max_bytes must be a whole number of bytes, 1 or more")
        if not _is_whole(self.keep) or self.keep < 0:
            raise ValueError("decision.log.keep must be a whole number, 0 or more")

    @classmethod
    def from_agent_config(cls, config: Mapping[str, Any] | None) -> DecisionLogConfig:
        """Read ``decision.log`` from an agent's config entry; the defaults
        where it, or the ``decision`` block, is absent."""
        log = _mapping(_mapping((config or {}).get("decision"), "decision").get("log"),
                       "decision.log")
        if unknown := log.keys() - _LOG_KEYS:
            raise ValueError(f"decision.log has no key {sorted(unknown)}")
        return cls(**log)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class DecisionLog:
    """One agent's decision file: one line per record, rotated at the cap."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = DecisionLogConfig.max_bytes,
        keep: int = DecisionLogConfig.keep,
        redactor: Redactor | None = None,
    ) -> None:
        DecisionLogConfig(dir=os.fspath(path), max_bytes=max_bytes, keep=keep)
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._keep = keep
        self._redactor = redactor if redactor is not None else SecretRedactor()
        self._lock = threading.Lock()

    def write(self, record: DecisionRecord) -> None:
        """Append *record* as one line, masked first. Raises on failure:
        :class:`DecisionRecorder` counts it and goes on."""
        masked = self._redactor.redact(json.loads(record.to_json()))
        line = json.dumps(masked, sort_keys=True, separators=(",", ":"), allow_nan=False)
        data = (line + "\n").encode("utf-8")
        with self._lock:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._rotate_for(len(data))
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

    def _rotate_for(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size == 0 or size + incoming <= self._max_bytes:
            return
        if self._keep == 0:
            self._path.unlink()
            return
        for n in range(self._keep - 1, 0, -1):
            if (older := self._rotated(n)).exists():
                older.replace(self._rotated(n + 1))
        self._path.replace(self._rotated(1))

    def _rotated(self, n: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{n}")


class DecisionRecorder:
    """Sends each decision an agent records to its log and its span (§D)."""

    def __init__(self, agent_id: str, log: DecisionLog | None) -> None:
        self._agent_id = agent_id
        self._log = log

    @classmethod
    def for_agent(
        cls, agent_id: str, config: Mapping[str, Any] | None, *, recording: bool,
    ) -> DecisionRecorder:
        """The recorder for *agent_id*, from its config entry. *recording*
        says whether recording is on for it; with it off, nothing is read
        and nothing will be written."""
        if not recording:
            return cls(agent_id, None)
        if not _AGENT_ID.fullmatch(agent_id):
            raise ValueError(f"agent id {agent_id!r} cannot name a decision log file")
        settings = DecisionLogConfig.from_agent_config(config)
        path = Path(settings.dir) / f"{agent_id}.jsonl"
        return cls(agent_id, DecisionLog(path, max_bytes=settings.max_bytes, keep=settings.keep))

    @property
    def recording(self) -> bool:
        return self._log is not None

    def record(self, record: DecisionRecord) -> None:
        """Send *record* to both sinks while recording is on. Never raises:
        a sink that fails is counted and logged, and the turn goes on."""
        if self._log is None:
            return
        try:
            self._log.write(record)
        except Exception as exc:
            self._failed("log", exc)
        try:
            span = _tracer.start_span(DECISION_CHECKPOINT_SPAN,
                                      attributes=self._span_attributes(record))
            span.end()
        except Exception as exc:
            self._failed("span", exc)
        # A record whose fields are not their types has been counted by a sink.
        with contextlib.suppress(Exception):
            chosen = record.outcome.action_class
            _metrics_decision.record_decision(
                agent_id=self._agent_id,
                checkpoint=record.context.checkpoint.value,
                action_class=chosen.value if chosen is not None else "none",
            )

    def _span_attributes(self, record: DecisionRecord) -> dict[str, Any]:
        outcome = record.outcome
        attributes: dict[str, Any] = {
            "agent.id": self._agent_id,
            "decision.checkpoint": record.context.checkpoint.value,
            "decision.candidate_count": len(record.candidates),
            "decision.record_id": record.record_id,
        }
        if outcome.action_class is not None:
            attributes["decision.action_class"] = outcome.action_class.value
        if outcome.confidence is not None:
            attributes["decision.confidence"] = outcome.confidence
        return attributes

    def _failed(self, sink: str, exc: Exception) -> None:
        # The error's message may quote the turn (PR 2 review F-1): its type only.
        logger.warning("decision %s for %s failed: %s", sink, self._agent_id,
                       type(exc).__name__)
        _metrics_decision.record_sink_error(agent_id=self._agent_id, sink=sink)


__all__ = ["DecisionLog", "DecisionLogConfig", "DecisionRecorder"]
