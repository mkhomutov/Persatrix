"""The files a run keeps, whole after any stop.

A run is stopped by Ctrl-C, a crash or a power cut, and started again from
what it kept, so each file it keeps must be whole when it is read back:

- :func:`write_json` replaces a file in one step: a stop leaves the old file
  or the new, never part of either, even when the machine itself stops;
- :func:`append_line` adds one JSON line, on the disk before it returns;
- :func:`sole_run` holds a directory for one run at a time, since a second
  run at once would hold or ask again what the first is holding or asking.

Text is UTF-8 whatever the machine's locale, since the records name arm D′.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOCK = "lock"


def write_json(path: Path, doc: Any) -> None:
    """Write *doc* as JSON in one step, so a stop leaves the old file or the
    new: the new one is on the disk before it takes the old one's name, and
    the name is on the disk before this returns."""
    part = path.with_name(f"{path.name}.part")
    _write_all(part, json.dumps(doc, indent=1, ensure_ascii=False).encode(), os.O_TRUNC)
    os.replace(part, path)
    _sync_directory(path.parent)


def append_line(path: Path, doc: Any) -> None:
    """Add *doc* to *path* as one JSON line, written out in full and on the
    disk before it returns. The line is ASCII, as the call log's are, so
    any reader decodes it alike."""
    _write_all(path, (json.dumps(doc) + "\n").encode(), os.O_APPEND)


@contextmanager
def sole_run(
    directory: Path, doing: str, refused: type[Exception] = RuntimeError,
) -> Iterator[None]:
    """Hold *directory* for one run, which is *doing* what it names; a second
    run at once is *refused*. The lock goes with the process, so a crash
    leaves nothing to clear."""
    fd = os.open(directory / LOCK, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise refused(f"{directory}: another run is {doing}") from None
        yield
    finally:
        os.close(fd)


def _write_all(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | mode, 0o644)
    try:
        while data:  # a write can stop short, on a nearly full disk for one
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
