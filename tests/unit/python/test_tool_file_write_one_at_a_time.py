"""Writes to one path happen one at a time (ISSUE-0215, point 2).

``file_write`` writes in a worker thread, so that a slow write does not
stop the agent.  Two things followed from the move that were not so
while the write held the event loop:

* Two calls for one path that overlap ran in two threads at once, and
  could leave a file that was neither text, both reporting success.
* A call cancelled while its thread was still writing returned at once;
  a retry could then finish first and be replaced, a moment later, by
  the text of the call that had been cancelled.

``write_text_file`` now takes a lock for the path it writes.  A write
waits for the one before it on the same path, a cancelled one included,
and writes to different paths still run side by side.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agents.tools import bounded_io, builtin

from . import test_tool_file_write_thread as _thread_tests

# The other file's fixture: the tool with a workspace under tmp_path.
_tools = _thread_tests._tools

#: Every wait these tests make on a write, so that a write that never
#: comes fails the test and does not hang it.
LIMIT = 5.0


class _Overlap:
    """Counts how many ``Path.write_text`` calls are under way at once."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, hold: float) -> None:
        self.most = 0
        self._now = 0
        self._guard = threading.Lock()
        real = Path.write_text

        def _counted(path: Path, *args: Any, **kwargs: Any) -> Any:
            with self._guard:
                self._now += 1
                self.most = max(self.most, self._now)
            try:
                time.sleep(hold)
                return real(path, *args, **kwargs)
            finally:
                with self._guard:
                    self._now -= 1

        monkeypatch.setattr(Path, "write_text", _counted)


async def test_two_writes_to_one_path_never_run_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlap = _Overlap(monkeypatch, hold=0.05)
    target = tmp_path / "shared.txt"
    first, second = await asyncio.wait_for(
        asyncio.gather(
            builtin.file_write(str(target), "the first text"),
            builtin.file_write(str(target), "the second"),
        ),
        LIMIT,
    )
    assert (first.success, second.success) == (True, True)
    assert overlap.most == 1, "two writes to one path were under way together"
    assert target.read_text() in {"the first text", "the second"}
    assert bounded_io._path_locks == {}, "a lock is kept only while a write wants it"


async def test_writes_to_different_paths_still_run_side_by_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is for a path, not for the tool: a slow write to one file
    does not hold up a write to another."""
    overlap = _Overlap(monkeypatch, hold=0.2)
    one, other = await asyncio.wait_for(
        asyncio.gather(
            builtin.file_write(str(tmp_path / "one.txt"), "1"),
            builtin.file_write(str(tmp_path / "other.txt"), "2"),
        ),
        LIMIT,
    )
    assert (one.success, other.success) == (True, True)
    assert overlap.most == 2, "a write waited for a write to another path"
    assert bounded_io._path_locks == {}


async def test_a_third_write_waits_for_the_second_after_the_first_has_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path keeps its lock for as long as any write wants it.  One
    write ends while a second waits; a third that arrives then queues
    behind the second, and does not start beside it with a lock of its
    own."""
    target = tmp_path / "queue.txt"
    started = {text: threading.Event() for text in "AB"}
    let_go = {text: threading.Event() for text in "AB"}
    under_way: list[str] = []
    together: list[list[str]] = []
    real = Path.write_text

    def _held(path: Path, data: str, *args: Any, **kwargs: Any) -> Any:
        under_way.append(data)
        together.append(list(under_way))
        try:
            if data in started:
                started[data].set()
                let_go[data].wait(timeout=LIMIT)
            return real(path, data, *args, **kwargs)
        finally:
            under_way.remove(data)

    monkeypatch.setattr(Path, "write_text", _held)

    async def _reached(event: threading.Event) -> None:
        assert await asyncio.to_thread(event.wait, LIMIT), "a write never started"

    first = asyncio.create_task(builtin.file_write(str(target), "A"))
    await _reached(started["A"])
    second = asyncio.create_task(builtin.file_write(str(target), "B"))
    await asyncio.sleep(0.1)  # the second is waiting for the first
    let_go["A"].set()
    await asyncio.wait_for(first, LIMIT)
    await _reached(started["B"])
    third = asyncio.create_task(builtin.file_write(str(target), "C"))
    await asyncio.sleep(0.1)  # time for the third to write, if nothing made it wait
    let_go["B"].set()
    results = await asyncio.wait_for(asyncio.gather(second, third), LIMIT)

    assert all(result.success for result in results)
    assert together == [["A"], ["B"], ["C"]], "writes to one path were under way together"
    assert target.read_text() == "C"
    assert bounded_io._path_locks == {}


async def test_two_real_writes_at_once_leave_one_text_whole(tmp_path: Path) -> None:
    """Nothing is slowed or patched here: a long text and a short one are
    written to one path at the same moment, many times.  The file is one
    of the two each time, never a mix and never empty."""
    long_text, short_text = "A" * 60_000, "B" * 20
    target = tmp_path / "raced.txt"
    neither = 0
    for _ in range(300):
        first, second = await asyncio.wait_for(
            asyncio.gather(
                builtin.file_write(str(target), long_text),
                builtin.file_write(str(target), short_text),
            ),
            LIMIT,
        )
        assert (first.success, second.success) == (True, True)
        neither += target.read_text() not in {long_text, short_text}
    assert neither == 0, f"{neither} of 300 files were neither text"


async def test_a_cancelled_write_cannot_replace_the_write_that_follows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write is slow, and its call is cancelled while the thread is
    still at it.  The retry waits for that thread, then writes: the file
    ends as the retry, and stays so."""
    target = tmp_path / "retried.txt"
    let_first_go = threading.Event()
    first_started = threading.Event()
    first_done = threading.Event()
    real = Path.write_text

    def _slow_first(path: Path, data: str, *args: Any, **kwargs: Any) -> Any:
        if not data.startswith("FIRST"):
            return real(path, data, *args, **kwargs)
        first_started.set()
        try:
            let_first_go.wait(timeout=LIMIT)
            return real(path, data, *args, **kwargs)
        finally:
            first_done.set()

    monkeypatch.setattr(Path, "write_text", _slow_first)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(builtin.file_write(str(target), "FIRST ATTEMPT, LONGER TEXT"), 0.05)
    assert first_started.is_set(), "the first write never reached its thread"
    assert not target.exists()

    retry = asyncio.create_task(builtin.file_write(str(target), "retry"))
    # Time for the retry to finish, if nothing made it wait.
    await asyncio.sleep(0.2)
    let_first_go.set()
    result = await asyncio.wait_for(retry, LIMIT)
    assert await asyncio.to_thread(first_done.wait, LIMIT), "the cancelled write never ended"

    assert result.success is True
    assert target.read_text() == "retry"
    assert bounded_io._path_locks == {}


async def test_a_write_that_fails_lets_the_next_one_to_that_path_through(tmp_path: Path) -> None:
    """The lock is given back when a write raises."""
    folder = tmp_path / "a-folder"
    folder.mkdir()
    for _ in range(2):
        result = await asyncio.wait_for(builtin.file_write(str(folder), "x"), LIMIT)
        assert (result.success, result.error_type) == (False, "OSError")
    assert bounded_io._path_locks == {}


async def test_a_text_that_is_not_utf8_fails_and_is_not_written_another_way(tmp_path: Path) -> None:
    """A lone surrogate cannot be written as UTF-8.  The write fails, as
    it did on the event loop, and nothing is put in the character's
    place: the file is left empty."""
    target = tmp_path / "surrogate.txt"
    result = await asyncio.wait_for(builtin.file_write(str(target), "before \ud800 after"), LIMIT)
    assert (result.success, result.error_type) == (False, "UnicodeEncodeError")
    assert target.read_bytes() == b""
    assert bounded_io._path_locks == {}
