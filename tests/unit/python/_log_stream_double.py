"""A scripted stand-in for the orchestrator's end of the log stream.

Shared by the log shipper's tests (ISSUE-0207).  It stands where
``LogServiceStub`` does: ``StreamLogs()`` returns a call the shipper
writes batches to and reads acknowledgements from, and each call follows
a :class:`Stream` script.  No network is involved.

The leading underscore keeps pytest from collecting this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from agents.generated import log_service_pb2 as logpb

__all__ = ["Orchestrator", "Stream"]



@dataclass
class Stream:
    """How one ``StreamLogs`` call behaves."""

    #: The write (counted from 0) that fails, as when nothing listens.
    fail_write_at: int | None = None
    #: When the orchestrator acknowledges: after each batch; after each
    #: batch but arriving only as the next write starts ("late"); only once
    #: it has read the whole stream; or never.
    acks: Literal["each", "late", "end", "none"] = "each"
    #: After the shipper's last batch: the orchestrator finishes reading
    #: and ends the stream, ends it with an error, or never ends it.
    end: Literal["clean", "error", "hang"] = "clean"
    #: How long the orchestrator takes to finish reading.
    end_delay: float = 0.0
    #: Hold the first write until the test sets ``Orchestrator.release``.
    hold_first_write: bool = False


class Call:
    """One call: what ``stub.StreamLogs()`` returns."""

    def __init__(self, script: Stream, orchestrator: Orchestrator) -> None:
        self._script = script
        self._orchestrator = orchestrator
        self._writes = 0
        self._count = 0
        self._acks: list[int] = []
        self._half_closed = False
        self._broken = False
        self._changed = asyncio.Event()
        self._final_sent = False
        self.cancelled = False

    async def write(self, batch: logpb.LogBatch) -> None:
        index = self._writes
        self._writes += 1
        # A real write waits on the network, which gives the event loop a
        # turn: the acknowledgement reader runs between two writes.
        await asyncio.sleep(0)
        if index == 0 and self._script.hold_first_write:
            await self._orchestrator.release.wait()
        if self._script.acks == "late" and self._count:
            # The acknowledgement of the batch before this one arrives now,
            # with no turn of the loop before what follows.
            self._acks.append(self._count)
            self._changed.set()
        if self._script.fail_write_at == index:
            # A call whose write fails is over: its reader ends too.
            self._broken = True
            self._changed.set()
            raise ConnectionError("failed to connect to all addresses")
        self._orchestrator.received.extend(e.message for e in batch.entries)
        self._count += len(batch.entries)
        if self._script.acks == "each":
            self._acks.append(self._count)
            self._changed.set()

    async def done_writing(self) -> None:
        self._half_closed = True
        self._changed.set()

    def __aiter__(self) -> Call:
        return self

    async def __anext__(self) -> logpb.LogAck:
        try:
            while True:
                if self._acks:
                    return logpb.LogAck(received_through_seq=self._acks.pop(0))
                if self._broken:
                    raise ConnectionError("stream reset")
                if self._half_closed:
                    return await self._end()
                self._changed.clear()
                await self._changed.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def _end(self) -> logpb.LogAck:
        """What the reader gets once the shipper has sent its last batch."""
        if not self._final_sent:
            if self._script.end == "hang":
                await asyncio.Event().wait()
            await asyncio.sleep(self._script.end_delay)
            if self._script.end == "error":
                raise ConnectionError("stream reset")
            self._final_sent = True
            if self._script.acks != "none":
                return logpb.LogAck(received_through_seq=self._count)
        self._orchestrator.clean_ends += 1
        raise StopAsyncIteration


@dataclass
class Orchestrator:
    """Every stream the shipper opens, in order; the last script repeats."""

    scripts: list[Stream]
    received: list[str] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    clean_ends: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def StreamLogs(self) -> Call:  # noqa: N802 — the stub's method name.
        script = self.scripts[min(len(self.calls), len(self.scripts) - 1)]
        call = Call(script, self)
        self.calls.append(call)
        return call
