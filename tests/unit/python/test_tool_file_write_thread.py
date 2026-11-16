"""``file_write`` writes off the event loop (ISSUE-0215, point 2).

The tool used to call ``Path.write_text`` in its own coroutine.  A write
blocks, so for as long as it took, on a slow disk or with a large
content, no other coroutine of the agent ran: no heartbeat, no other
tool call, no reply to the orchestrator.  ``file_read`` was moved to a
worker thread for the same reason; this is the write's turn.

What the tool checks, returns and reports as an error is unchanged, and
pinned here beside the move.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agents.tools import builtin
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry
from agents.tools.sandbox import PathValidator


@pytest.fixture(autouse=True)
def _tools(tmp_path: Path) -> Iterator[None]:
    clear_registry()
    importlib.reload(builtin)
    builtin.permission_gate = PermissionGate({
        "filesystem": {"write": [str(tmp_path / "**")]},
    })
    builtin.path_validator = PathValidator(
        allow_read=[], allow_write=[str(tmp_path / "**")], deny=[str(tmp_path / "denied" / "**")],
    )
    builtin.workspace_root = tmp_path
    yield
    builtin.permission_gate = None
    builtin.path_validator = None
    builtin.workspace_root = None
    clear_registry()


@pytest.mark.parametrize("step", ["write_text", "mkdir"])
async def test_a_slow_write_lets_the_agents_other_coroutines_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str,
) -> None:
    """The write, and the making of its folder, are held until another
    coroutine on the same loop lets them go.  On the loop's own thread
    nothing can: the step gives up after three seconds and says so."""
    target = tmp_path / "new" / "file.txt"
    started = threading.Event()
    let_go = threading.Event()
    let_go_in_time: list[bool] = []
    real = getattr(Path, step)

    def _slow(self: Path, *args: Any, **kwargs: Any) -> Any:
        started.set()
        let_go_in_time.append(let_go.wait(timeout=3))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, step, _slow)

    async def _another_coroutine() -> None:
        # Five seconds at most, so a tool that never reaches the step
        # fails the test below and does not hang it.
        for _ in range(1_000):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        let_go.set()

    result, _ = await asyncio.gather(
        builtin.file_write(str(target), "content"), _another_coroutine(),
    )

    assert started.is_set(), f"the tool never called Path.{step}"
    assert all(let_go_in_time), "nothing else ran while the write was under way"
    assert (result.success, result.data) == (True, f"Wrote 7 bytes to {target}")
    assert target.read_text(encoding="utf-8") == "content"


async def test_the_write_and_its_folder_are_made_off_the_loops_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "a" / "b" / "file.txt"
    ran_on: dict[str, int] = {}

    def _spy_on(step: str) -> None:
        real = getattr(Path, step)

        def _spy(self: Path, *args: Any, **kwargs: Any) -> Any:
            ran_on[step] = threading.get_ident()
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, step, _spy)

    _spy_on("write_text")
    _spy_on("mkdir")

    result = await builtin.file_write(str(target), "x")
    assert result.success is True
    assert set(ran_on) == {"write_text", "mkdir"}
    assert threading.get_ident() not in ran_on.values()


async def test_what_the_write_returns_is_unchanged(tmp_path: Path) -> None:
    """The count is of UTF-8 bytes, the folders are made, and the file is
    replaced whole, as before the move."""
    target = tmp_path / "deep" / "er" / "note.txt"
    first = await builtin.file_write(str(target), "a much longer first text")
    second = await builtin.file_write(str(target), "žluťoučký")
    assert (first.success, first.data) == (True, f"Wrote 24 bytes to {target}")
    assert (second.success, second.data) == (True, f"Wrote 13 bytes to {target}")
    assert target.read_bytes() == "žluťoučký".encode()


@pytest.mark.parametrize(
    ("make", "error_starts"),
    [
        # The parent is a file: the folder cannot be made.
        (lambda root: (root / "plain").write_text("x") and root / "plain" / "file.txt", "[Errno"),
        # The path is a folder: it cannot be written as a file.
        (lambda root: (root / "folder").mkdir() or root / "folder", "[Errno"),
    ],
    ids=["the parent is a file", "the path is a folder"],
)
async def test_an_error_from_the_worker_thread_is_reported_as_before(
    tmp_path: Path, make: Any, error_starts: str,
) -> None:
    target = make(tmp_path)
    result = await builtin.file_write(str(target), "x")
    assert (result.success, result.error_type) == (False, "OSError")
    assert result.error is not None and result.error.startswith(error_starts)
    assert str(target) in result.error or str(target.parent) in result.error


async def test_a_refused_path_is_refused_before_any_thread_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checks come first and are unchanged: a denied path and a path
    outside the allowlist never reach the write."""
    used: list[Any] = []

    async def _no_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        used.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _no_thread)
    denied = await builtin.file_write(str(tmp_path / "denied" / "x.txt"), "x")
    outside = await builtin.file_write(str(tmp_path.parent / "outside.txt"), "x")
    assert (denied.success, denied.error_type) == (False, "PermissionError")
    assert (outside.success, outside.error_type) == (False, "PermissionError")
    assert used == []
    assert not (tmp_path / "denied").exists()
