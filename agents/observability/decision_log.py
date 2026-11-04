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

**What a line holds.** The record has no field for text from the turn: a
candidate's message, reply or tool-argument values appear only as its
SHA-256 ``content_hash`` (PR 1), beside its class, ids, tool name and
argument names, and the context's acting channel and protection level. Its
other strings (ids, a target, argument names, an outcome's rationale) are
written as the checkpoint fills them, masked for secrets only, so the
checkpoints (PRs 5 to 7) must keep the turn's text out of them. The hash is
not keyed, so a short or guessable text — a yes or no, a number, a channel
name — can be found by hashing guesses (PR 1 review F-14). That limit is
recorded rather than keyed: the file is readable by its owner only, the same
owner whose memory store holds the texts themselves, and a key would stop
replay and the eval harness matching a hash to a known text.

**Size.** When the next line would take the file past ``max_bytes``, the
file becomes ``.1`` (``.1`` becomes ``.2``, and so on up to ``keep``; the
oldest, and any file above a lowered ``keep``, is dropped), and ``keep: 0``
drops the full file. A line longer than the cap is still written, alone in a
new file. The settings are the agent's ``decision.log`` keys: ``dir``
(default ``data/decisions``), ``max_bytes`` (10 MiB) and ``keep`` (5, at most
100). Every log on one file in a process shares one lock; a second process
writing the same file is not guarded against.

**The file.** The folder is created owner-only (0700); one that exists keeps
its mode. The file is created owner-only (0600), and one found open to
others is made owner-only. A link at the file's path, or a file of another
user, is refused. A line is written whole or not at all: a short write is
carried on, and a write that fails part-way is cut back off.

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
import stat
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

#: The canonical agent id, which is also a safe file name. At 200
#: characters, the last rotated file's name stays well under the 255 bytes
#: most file systems allow.
_AGENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,198}[a-z0-9]")
_LOG_KEYS = frozenset({"dir", "max_bytes", "keep"})
#: Rotation renames each kept file, under the lock, on the turn's thread.
_MAX_KEEP = 100
#: A rotated file's number.
_ROTATED = re.compile(r"[1-9][0-9]*")

#: One lock per log file, shared by every DecisionLog on it in this process:
#: two recorders for one agent rotate the same files.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


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
        if not _is_whole(self.keep) or not 0 <= self.keep <= _MAX_KEEP:
            raise ValueError(f"decision.log.keep must be a whole number from 0 to {_MAX_KEEP}")

    @classmethod
    def from_agent_config(cls, config: Mapping[str, Any] | None) -> DecisionLogConfig:
        """Read ``decision.log`` from an agent's config entry; the defaults
        where it, or the ``decision`` block, is absent."""
        log = _mapping(_mapping((config or {}).get("decision"), "decision").get("log"),
                       "decision.log")
        if not all(isinstance(key, str) for key in log):
            raise ValueError("decision.log's keys must be names")
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
        with _locks_guard:
            self._lock = _locks.setdefault(os.path.abspath(self._path), threading.Lock())

    def write(self, record: DecisionRecord) -> None:
        """Append *record* as one line, masked first. Raises on failure:
        :class:`DecisionRecorder` counts it and goes on."""
        masked = self._redactor.redact(json.loads(record.to_json()))
        line = json.dumps(masked, sort_keys=True, separators=(",", ":"), allow_nan=False)
        data = (line + "\n").encode("utf-8")
        with self._lock:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._rotate_for(len(data))
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
            fd = os.open(self._path, flags, 0o600)
            try:
                _own_file(fd)
                _append_whole(fd, data)
            finally:
                os.close(fd)

    def _rotate_for(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size == 0 or size + incoming <= self._max_bytes:
            return
        # Oldest first, from one listing of the folder, so a file above a
        # lowered keep goes too.
        for n in sorted(self._rotated_numbers(), reverse=True):
            if n >= self._keep:
                self._rotated(n).unlink()
            else:
                self._rotated(n).replace(self._rotated(n + 1))
        if self._keep == 0:
            self._path.unlink()
        else:
            self._path.replace(self._rotated(1))

    def _rotated_numbers(self) -> list[int]:
        prefix = f"{self._path.name}."
        return [int(path.name[len(prefix):]) for path in self._path.parent.iterdir()
                if path.name.startswith(prefix) and _ROTATED.fullmatch(path.name[len(prefix):])]

    def _rotated(self, n: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{n}")


def _own_file(fd: int) -> None:
    """Refuse an open log that is not a plain file of this user's, and make
    one open to others owner-only."""
    status = os.fstat(fd)
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid():
        raise PermissionError("the decision log is not this user's own file")
    if status.st_mode & 0o077:
        os.fchmod(fd, 0o600)


def _append_whole(fd: int, data: bytes) -> None:
    """Write all of *data*, carrying on after a short write. A write that
    fails part-way is cut back off, so the file always ends on a whole line,
    and the error is raised."""
    start = os.fstat(fd).st_size
    view = memoryview(data)
    written = 0
    try:
        while written < len(data):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("the decision log took no bytes")
            written += n
    except BaseException:
        if written:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, start)
        raise


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
