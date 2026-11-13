"""The shell, HTTP and file-read tools hold no more of an output than
they will show (ISSUE-0215).

Each of the three read everything, a command's whole output, a whole
response body, a whole file, and only then cut it to the 100 KB cap.  The
cap limited what the model saw, not what the agent process held: one
command printing 300 MB took the process from 70 MB to 1.3 GB.

These tests pin three things:

* memory: a call that handles tens of megabytes allocates a small, fixed
  amount in the agent (measured with ``tracemalloc``, which sees the
  agent's own allocations and not the command's);
* the result: what comes back is exactly what the old read-then-cut code
  returned, checked against that formula, with the notice in its place;
* the limits around it: exit codes, the time limit, and both output
  streams read at once.

``http_request``'s tests are in ``test_tool_bounded_http_body.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shlex
import sys
import threading
import tracemalloc
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from agents.tools import bounded_io, builtin
from agents.tools.builtin import MAX_OUTPUT_BYTES
from agents.tools.permissions import PermissionGate
from agents.tools.registry import ToolResult, clear_registry
from agents.tools.sandbox import PathValidator, truncate_text

_PY = shlex.quote(sys.executable)
MB = 1024 * 1024
NOTICE = "\n... [truncated at 100 KB]"

#: The most a call may allocate in the agent while it handles output far
#: over the cap.  The fixed code peaks near 1 MB; the old code held the
#: whole output, several times over.
MEMORY_BUDGET = 6 * MB


@pytest.fixture(autouse=True)
def _tools(tmp_path: Path) -> Iterator[None]:
    clear_registry()
    importlib.reload(builtin)
    builtin.permission_gate = PermissionGate({
        "filesystem": {"read": [str(tmp_path / "**")]},
        "shell": {"allowed_commands": [sys.executable]},
        "network": {"allow": ["127.0.0.1"]},
    })
    builtin.path_validator = PathValidator(
        allow_read=[str(tmp_path / "**")], allow_write=[], deny=[],
    )
    builtin.workspace_root = tmp_path
    yield
    builtin.permission_gate = None
    builtin.path_validator = None
    builtin.workspace_root = None
    clear_registry()


async def _measured(call: Callable[[], Awaitable[ToolResult]]) -> tuple[ToolResult, int]:
    """The call's result and the peak the agent allocated while it ran."""
    tracemalloc.start()
    try:
        result = await call()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _old_cut(raw: bytes) -> str:
    """What the read-everything code returned for a stream of *raw* bytes."""
    return truncate_text(raw.decode("utf-8", errors="replace"), MAX_OUTPUT_BYTES)


def _python(code: str) -> str:
    return f"{_PY} -c {shlex.quote(code)}"


def _printer(megabytes: int, *, stream: str = "stdout", exit_code: int = 0) -> str:
    return _python(
        f"import sys; b = b'x' * {MB}\n"
        f"for _ in range({megabytes}): sys.{stream}.buffer.write(b)\n"
        f"sys.exit({exit_code})",
    )


# ─── The reader itself ───────────────────────────────────────────────


async def test_the_reader_keeps_the_cap_plus_one_byte_and_drains_the_rest() -> None:
    stream = asyncio.StreamReader()
    total = 0
    for i in range(50):
        chunk = bytes([65 + i % 26]) * 10_000
        stream.feed_data(chunk)
        total += len(chunk)
    stream.feed_eof()

    kept = await bounded_io.drain_stream(stream, 1_001)

    assert kept == b"A" * 1_001
    assert stream.at_eof(), "the input was not read to its end"
    assert total == 500_000


async def test_the_reader_returns_a_short_stream_whole() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"all of it")
    stream.feed_eof()
    assert await bounded_io.drain_stream(stream, 1_001) == b"all of it"


@pytest.mark.parametrize("cap", [16, 17, 18, 19, 64])
def test_keeping_one_byte_past_the_cap_gives_the_same_cut(cap: int) -> None:
    """Why cap plus one byte is enough.  The extra byte says the stream
    ran over; a character split at the end of what was kept decodes to a
    replacement that lies past the cap and is cut away with it."""
    samples = [
        b"a" * (cap + 40),
        b"a" * (cap - 1) + "\u20ac".encode() * 9,          # 3-byte character on the cut
        b"a" * (cap - 2) + "\U0001f600".encode() * 9,      # 4-byte character on the cut
        b"a" * (cap - 3) + "\U0001f600".encode() * 9,
        b"a" * (cap - 1) + "\u00e9".encode() * 9,          # 2-byte character on the cut
        b"\xff" * (cap + 5),                              # every byte invalid
        b"a" * (cap - 1) + b"\xf0\x9f" + b"b" * 20,       # a broken character on the cut
        "\u20ac".encode() * (cap // 3) + b"\x80" + b"z" * 30,
        b"a" * cap,                                       # exactly the cap: not cut
        b"a" * (cap + 1),
    ]
    for raw in samples:
        whole = truncate_text(raw.decode("utf-8", errors="replace"), cap)
        bounded = truncate_text(raw[: cap + 1].decode("utf-8", errors="replace"), cap)
        assert bounded == whole, raw


# ─── shell_exec ──────────────────────────────────────────────────────


@pytest.mark.parametrize("exit_code", [0, 3])
async def test_a_command_that_floods_stdout_is_cut_and_keeps_its_exit_code(
    exit_code: int,
) -> None:
    result, peak = await _measured(
        lambda: builtin.shell_exec(_printer(24, exit_code=exit_code), timeout=20),
    )
    assert result.success is (exit_code == 0)
    assert result.data["exit_code"] == exit_code
    assert result.data["stdout"] == "x" * MAX_OUTPUT_BYTES + NOTICE
    assert result.data["stderr"] == ""
    if exit_code:
        assert result.error == f"Command exited with code {exit_code}"
    assert peak < MEMORY_BUDGET, f"the agent allocated {peak / MB:.1f} MB"


async def test_a_command_that_closes_its_output_before_it_exits_keeps_its_exit_code() -> None:
    """Both streams reach their end while the command is still running.
    The tool has to wait for the command itself, or it has no exit code
    to report."""
    code = "import os, time\nos.close(1)\nos.close(2)\ntime.sleep(0.4)\nos._exit(3)"
    result = await builtin.shell_exec(_python(code), timeout=15)
    assert result.data["exit_code"] == 3
    assert result.success is False
    assert result.error == "Command exited with code 3"


async def test_a_command_that_floods_stderr_is_cut_too() -> None:
    result, peak = await _measured(
        lambda: builtin.shell_exec(_printer(24, stream="stderr"), timeout=20),
    )
    assert result.success is True
    assert result.data["stderr"] == "x" * MAX_OUTPUT_BYTES + NOTICE
    assert peak < MEMORY_BUDGET, f"the agent allocated {peak / MB:.1f} MB"


async def test_both_streams_are_read_at_once() -> None:
    """A pipe nobody reads fills, and the command blocks on its next
    write.  Reading stdout to its end before stderr would stall on a
    command that writes a lot to stderr first."""
    code = (
        f"import sys; b = b'e' * {MB}\n"
        "for _ in range(4): sys.stderr.buffer.write(b)\n"
        "sys.stderr.flush()\n"
        "for _ in range(4): sys.stdout.buffer.write(b'o' * 1048576)\n"
        "sys.stdout.flush()\n"
        "for _ in range(4): sys.stderr.buffer.write(b)\n"
    )
    result = await builtin.shell_exec(_python(code), timeout=15)
    assert result.success is True, result.error
    assert result.data["stdout"].startswith("o" * 1000)
    assert result.data["stderr"].startswith("e" * 1000)


async def test_a_character_split_at_the_cut_reads_as_it_did() -> None:
    raw = b"a" * (MAX_OUTPUT_BYTES - 1) + "\u20ac".encode() * 5_000
    code = (
        f"import sys; sys.stdout.buffer.write(b'a' * {MAX_OUTPUT_BYTES - 1}"
        " + '\\u20ac'.encode() * 5000)"
    )
    result = await builtin.shell_exec(_python(code), timeout=15)
    assert result.data["stdout"] == _old_cut(raw)
    assert result.data["stdout"] == "a" * (MAX_OUTPUT_BYTES - 1) + NOTICE


@pytest.mark.parametrize("size", [0, 1, MAX_OUTPUT_BYTES - 1, MAX_OUTPUT_BYTES])
async def test_output_up_to_the_cap_comes_back_whole(size: int) -> None:
    code = f"import sys; sys.stdout.buffer.write(b'y' * {size})"
    result = await builtin.shell_exec(_python(code), timeout=15)
    assert result.data == {"stdout": "y" * size, "stderr": "", "exit_code": 0}


async def test_one_byte_over_the_cap_is_cut() -> None:
    code = f"import sys; sys.stdout.buffer.write(b'y' * {MAX_OUTPUT_BYTES + 1})"
    result = await builtin.shell_exec(_python(code), timeout=15)
    assert result.data["stdout"] == "y" * MAX_OUTPUT_BYTES + NOTICE


async def test_the_time_limit_stops_a_command_that_never_stops_printing() -> None:
    """Output past the cap is read and thrown away, so the command is not
    blocked on a full pipe and the time limit is what ends it."""
    code = (
        "import sys, time\n"
        f"b = b'x' * {MB}\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b); sys.stdout.flush(); time.sleep(0.02)\n"
    )
    result, peak = await _measured(lambda: builtin.shell_exec(_python(code), timeout=1))
    assert result.success is False
    assert result.error == "Command timed out after 1s"
    assert peak < MEMORY_BUDGET, f"the agent allocated {peak / MB:.1f} MB"


# ─── file_read ───────────────────────────────────────────────────────


async def test_a_large_file_is_cut_without_being_read_whole(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    with path.open("wb") as handle:
        for _ in range(24):
            handle.write(b"x" * MB)
    result, peak = await _measured(lambda: builtin.file_read(str(path)))
    assert result.success is True
    assert result.data == "x" * MAX_OUTPUT_BYTES + NOTICE
    assert peak < MEMORY_BUDGET, f"the agent allocated {peak / MB:.1f} MB"


_FILES: dict[str, bytes] = {
    "empty": b"",
    "ascii": b"hello\nworld\n",
    "crlf and lone cr": b"one\r\ntwo\rthree\n",
    "utf-8 with a bom": b"\xef\xbb\xbfcaf\xc3\xa9",
    "exactly the cap": b"z" * MAX_OUTPUT_BYTES,
    "one byte over": b"z" * (MAX_OUTPUT_BYTES + 1),
    "a 3-byte character on the cut": b"a" * (MAX_OUTPUT_BYTES - 1) + "\u20ac".encode() * 50,
    "a 4-byte character on the cut": b"a" * (MAX_OUTPUT_BYTES - 2) + "\U0001f600".encode() * 50,
    # Over the cap on disk, under it once line endings are translated: a
    # reader that stopped at the cap in raw bytes would drop the tail
    # and say nothing.
    "crlf file over the cap on disk only": b"line\r\n" * (MAX_OUTPUT_BYTES // 5 - 100),
    "crlf file over the cap either way": b"line\r\n" * (MAX_OUTPUT_BYTES // 2),
}


@pytest.mark.parametrize("name", _FILES)
async def test_a_file_reads_as_it_did(tmp_path: Path, name: str) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(_FILES[name])
    result = await builtin.file_read(str(path))
    assert result.success is True
    assert result.data == truncate_text(path.read_text(encoding="utf-8"), MAX_OUTPUT_BYTES)


async def test_a_binary_file_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff" * 100)
    result = await builtin.file_read(str(path))
    assert (result.success, result.error, result.error_type) == (
        False, f"Cannot read binary file: {path}", "UnicodeDecodeError",
    )


async def test_bytes_that_are_not_text_far_past_the_cap_are_not_seen(tmp_path: Path) -> None:
    """The one result that changes for a file over the cap.  The old code
    read the whole file and refused it as binary wherever the bad byte
    was.  A file is now read only as far as the cap needs, so a bad byte
    well past that is never reached and the text before it comes back
    cut, as for any long file."""
    path = tmp_path / "mostly-text.log"
    path.write_bytes(b"t" * (4 * MAX_OUTPUT_BYTES) + b"\xff\xfe")
    result = await builtin.file_read(str(path))
    assert result.success is True
    assert result.data == "t" * MAX_OUTPUT_BYTES + NOTICE


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions that bind the user running the tests",
)
@pytest.mark.parametrize("kind", ["directory", "unreadable file", "path under a file"])
async def test_a_path_that_cannot_be_read_fails_with_the_message_it_always_had(
    tmp_path: Path, kind: str,
) -> None:
    locked = tmp_path / "locked.txt"
    locked.write_text("secret", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    path = {
        "directory": tmp_path / "sub",
        "unreadable file": locked,
        "path under a file": tmp_path / "locked.txt" / "below",
    }[kind]
    if kind == "unreadable file":
        locked.chmod(0)
    try:
        with pytest.raises(OSError) as old:
            path.read_text(encoding="utf-8")
        result = await builtin.file_read(str(path))
    finally:
        locked.chmod(0o600)
    assert (result.success, result.error, result.error_type) == (
        False, str(old.value), "OSError",
    )


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="this platform has no named pipes in the file system",
)
async def test_a_named_pipe_is_refused_at_once(tmp_path: Path) -> None:
    """Opening a pipe to read waits for a writer, for ever if none comes.
    A size check would not catch it: a pipe's size reads as zero."""
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    stalled = threading.Event()

    def _unblock() -> None:
        # Only reached if the read is stuck: give it a writer so the test ends.
        stalled.set()
        with open(fifo, "wb"):
            pass

    watchdog = threading.Timer(3.0, _unblock)
    watchdog.start()
    try:
        result = await builtin.file_read(str(fifo))
    finally:
        watchdog.cancel()
        watchdog.join()

    assert not stalled.is_set(), "file_read waited on the pipe"
    assert (result.success, result.error, result.error_type) == (
        False, f"Not a regular file: {fifo}", "OSError",
    )


async def test_the_file_is_read_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking read on the loop's thread stops every other coroutine
    in the agent until it returns."""
    path = tmp_path / "file.txt"
    path.write_text("text", encoding="utf-8")
    read_on: list[int] = []
    real = bounded_io.read_text_file

    def _spy(*args: Any, **kwargs: Any) -> str:
        read_on.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(bounded_io, "read_text_file", _spy)
    result = await builtin.file_read(str(path))
    assert result.data == "text"
    assert read_on and read_on[0] != threading.get_ident()
