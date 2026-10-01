"""EXP-001 harness — the files a run keeps, whole after any stop (PR 6b).

A run is stopped by Ctrl-C, a crash or a power cut, and started again from
what it kept. So a file the harness replaces is the old one or the new,
never part of either, and a line it appends is on the disk before it goes
on. A directory one run holds refuses a second run at once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evaluators.exp001.files import append_line, sole_run, write_json


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each path os.fsync was given, in order, in place of syncing it."""
    paths: list[str] = []

    def fsync(fd: int) -> None:
        paths.append(os.readlink(f"/proc/self/fd/{fd}"))

    monkeypatch.setattr(os, "fsync", fsync)
    return paths


class TestWritingAFileWhole:
    def test_the_new_file_is_synced_before_it_replaces_the_old_and_the_directory_after(
        self, tmp_path: Path, synced: list[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without it a power cut can keep the rename and lose the data, so
        the run would find an empty file where its state was."""
        path = tmp_path / "pair.json"
        replace = os.replace
        replaced: list[int] = []

        def replace_after_sync(src: Path, dst: Path) -> None:
            replaced.append(len(synced))
            replace(src, dst)

        monkeypatch.setattr(os, "replace", replace_after_sync)
        write_json(path, {"arm": "D′"})
        assert synced == [str(tmp_path / "pair.json.part"), str(tmp_path)]
        assert replaced == [1]
        assert json.loads(path.read_bytes().decode("utf-8")) == {"arm": "D′"}

    def test_it_is_written_as_utf_8(self, tmp_path: Path) -> None:
        write_json(tmp_path / "run.json", {"arm": "D′"})
        assert "D′".encode() in (tmp_path / "run.json").read_bytes()


class TestAppendingALine:
    def test_each_line_is_synced_before_it_returns(
        self, tmp_path: Path, synced: list[str],
    ) -> None:
        path = tmp_path / "tries.jsonl"
        append_line(path, {"try": 1})
        append_line(path, {"try": 2, "arm": "D′"})
        assert synced == [str(path), str(path)]
        lines = path.read_bytes().decode("utf-8").splitlines()
        assert [json.loads(line) for line in lines] == [{"try": 1}, {"try": 2, "arm": "D′"}]


class TestOneRunAtATime:
    def test_a_second_run_at_once_is_refused(self, tmp_path: Path) -> None:
        with sole_run(tmp_path, "holding this practice run"):
            with pytest.raises(RuntimeError, match="another run is holding this practice run"):
                with sole_run(tmp_path, "holding this practice run"):
                    pass

    def test_the_refusal_can_be_the_callers_own(self, tmp_path: Path) -> None:
        class RefusedError(RuntimeError):
            pass

        with sole_run(tmp_path, "judging"), pytest.raises(RefusedError):
            with sole_run(tmp_path, "judging", RefusedError):
                pass

    def test_the_directory_is_free_once_the_run_ends(self, tmp_path: Path) -> None:
        with sole_run(tmp_path, "judging"):
            pass
        with sole_run(tmp_path, "judging"):
            pass
