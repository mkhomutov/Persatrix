"""Reads that keep no more than the built-in tools will show (ISSUE-0215).

``shell_exec``, ``http_request`` and ``file_read`` cut what they return
to an output cap.  They used to read everything first: a command's whole
output, a whole response body, a whole file.  The cap limited what the
model saw and not what the agent process held, so one command that
printed 300 MB took the process past a gigabyte.

Each reader here stops keeping at the cap plus one byte.  The one byte
over is how the caller knows there was more, and so adds its truncation
notice; the cut itself, and the notice, stay with
:func:`agents.tools.sandbox.truncate_text`.  What a tool returns for an
output under the cap is unchanged, byte for byte.

* A command's streams are read to their end and the rest thrown away, so
  the command is never blocked on a full pipe and still ends by itself,
  by its time limit or by its CPU limit, with its exit code.
* A response body is read only until the text is known to be over the
  cap.
* A file is read in a worker thread, and only if it is a regular file.
"""

from __future__ import annotations

import asyncio
import codecs
import errno
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

__all__ = [
    "NotARegularFileError",
    "communicate_bounded",
    "drain_stream",
    "read_body_text",
    "read_text_file",
]

#: How much is asked of a pipe or a socket at a time.
_CHUNK_BYTES = 64 * 1024


class NotARegularFileError(OSError):
    """The path names a pipe, a socket or a device, not a file to read."""


async def drain_stream(stream: asyncio.StreamReader, keep_bytes: int) -> bytes:
    """Read *stream* to its end and return its first *keep_bytes* bytes.

    Everything after them is read and dropped.  Stopping instead would
    leave the pipe to fill and the process writing to it blocked.
    """
    kept = bytearray()
    while chunk := await stream.read(_CHUNK_BYTES):
        room = keep_bytes - len(kept)
        if room > 0:
            kept += chunk[:room]
    return bytes(kept)


async def communicate_bounded(
    proc: asyncio.subprocess.Process, keep_bytes: int,
) -> tuple[bytes, bytes]:
    """``proc.communicate()`` that keeps *keep_bytes* of each stream.

    Both streams are read at the same time, as ``communicate`` reads
    them: a command that fills one pipe while only the other is being
    read would stall.  Returns once both are at their end and the
    process has exited.  The process must have been started with both
    streams piped.
    """
    if proc.stdout is None or proc.stderr is None:
        raise ValueError("the process's stdout and stderr must both be pipes")
    stdout, stderr = await asyncio.gather(
        drain_stream(proc.stdout, keep_bytes),
        drain_stream(proc.stderr, keep_bytes),
    )
    await proc.wait()
    return stdout, stderr


async def read_body_text(resp: aiohttp.ClientResponse, max_bytes: int) -> str:
    """The response body as text, read no further than *max_bytes* needs.

    Decodes as ``resp.text(errors="replace")`` does, in the charset the
    response names, and stops reading once the text is over *max_bytes*
    of UTF-8.  The text returned is then longer than the cap by less than
    one read, which is how the caller knows to cut it.  A body under the
    cap comes back whole and identical to ``resp.text``'s.

    The size is counted in the decoded text, not in the body's own
    bytes, because the two differ with the charset: a UTF-16 body is
    twice the size of the text the cap is measured on.
    """
    try:
        encoding = resp.get_encoding()
    except RuntimeError:
        # No charset in the headers.  aiohttp then asks the session's
        # fallback resolver, which needs the whole body; the default
        # resolver, the one ``http_request``'s session has, answers UTF-8.
        encoding = "utf-8"
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    pieces: list[str] = []
    size = 0
    while size <= max_bytes:
        chunk = await resp.content.read(_CHUNK_BYTES)
        if not chunk:
            pieces.append(decoder.decode(b"", final=True))
            break
        piece = decoder.decode(chunk)
        pieces.append(piece)
        size += len(piece.encode("utf-8", errors="replace"))
    return "".join(pieces)


def read_text_file(path: Path, max_bytes: int) -> str:
    """The start of a UTF-8 text file: enough to fill *max_bytes*, plus one character.

    Blocking; call it in a worker thread.  Reads as ``Path.read_text``
    does (strict UTF-8, line endings translated) and returns the whole
    text of a file that fits.  For a longer one it returns one character
    more than *max_bytes* characters, which is over the cap however few
    bytes each takes, so the caller cuts it and adds its notice.  The
    count is in characters, after translation, because a file with
    Windows line endings is longer on disk than the text it reads as.

    Raises :class:`NotARegularFileError` for a named pipe, a socket or a
    device.  A pipe would otherwise stall the read until something wrote
    to it, and its size reads as zero, so a size check does not find it.
    The check is made on the open file, not on the path, so the file
    cannot be swapped for a pipe in between; it is opened without
    waiting, which is what lets a pipe be opened at all.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            # POSIX opens a directory for reading; reading it is the error.
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
        if not stat.S_ISREG(mode):
            raise NotARegularFileError(f"Not a regular file: {path}")
        with open(fd, encoding="utf-8", closefd=False) as handle:
            return handle.read(max_bytes + 1)
    finally:
        os.close(fd)
