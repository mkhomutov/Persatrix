"""Pin the code-file warning in ``scripts.checks.file_size``.

Until ruling (e) of the sequencing Amendment 2026-09-12, one number did two
jobs: 500 lines was both the size worth splitting and the line a code file
failed the gate at.  That made 500 a cliff, and files piled up against it,
where every fix cost a split or a deleted comment.  The ruling keeps 500 as a
warning and moves the failure to 800.

Pinned here: the ruling's numbers, where the warning starts and where the
failure starts, that the warning never changes the exit code, that a failing
file is listed once, that both numbers can be moved by flag, and that the
near-cap band for code now sits under the failure instead of under the
warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.checks import file_size


def _code(root: Path, rel: str, lines: int) -> None:
    """Materialise a code file at *rel* that measures exactly *lines* lines."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines, encoding="utf-8")


def test_the_numbers_are_the_rulings() -> None:
    """500 and 800 come from the amendment, and so does leaving the word
    limits alone: changing any of them takes a new amendment, not a PR."""
    assert file_size.DEFAULT_WARN_CODE_LINES == 500
    assert file_size.DEFAULT_MAX_CODE_LINES == 800
    assert file_size.DEFAULT_MAX_DOC_WORDS == 3000
    assert file_size.DEFAULT_MAX_RFC_WORDS == 8000


def test_a_code_file_fails_only_past_800_lines(tmp_path: Path) -> None:
    _code(tmp_path, "past_warning.py", 501)
    _code(tmp_path, "at_limit.py", 800)
    _code(tmp_path, "over.py", 801)

    failures, _, _ = file_size._scan_files(tmp_path)

    assert [(f.file, f.limit) for f in failures] == [("over.py", 800)]


def test_a_code_file_past_500_lines_is_warned_longest_first() -> None:
    warned = file_size._code_warnings(
        [("at_warning.py", 500), ("past.py", 501), ("at_limit.py", 800)],
    )

    assert warned == [("at_limit.py", 800), ("past.py", 501)]


def test_a_failing_file_is_not_warned_as_well() -> None:
    """It is listed once, where it fails, so the output keeps passing and
    failing apart."""
    assert file_size._code_warnings([("over.py", 801)]) == []


def test_the_warning_never_changes_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A warning that failed the run would just be the old cliff again."""
    _code(tmp_path, "long.py", 600)

    assert file_size.check_file_size(tmp_path, strict=True) == 0
    out = capsys.readouterr().out
    assert "[OK] All files within size limits." in out
    assert "[WARN] 1 code file(s) over 500 lines" in out
    assert "long.py: 600 lines" in out


def test_a_failing_run_still_prints_the_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing run is when someone is reading this output, and the two
    lists have different tags, so a warning is never read as the failure."""
    _code(tmp_path, "long.py", 600)
    _code(tmp_path, "over.py", 900)

    assert file_size.check_file_size(tmp_path, strict=True) == 1
    out = capsys.readouterr().out
    assert "[OVER] 1 file(s) exceed size limits:" in out
    assert "over.py: 900 lines (limit: 800)" in out
    assert "[WARN] 1 code file(s) over 500 lines" in out
    assert "long.py: 600 lines" in out


def test_the_code_band_sits_under_the_failure_not_the_warning() -> None:
    """A file at 500 lines can take a one-line fix now, so it is not near a
    cap.  One at 776 of 800 is: 3 % of 800 is 24 lines."""
    notices = file_size._near_cap_notices(
        [("at_warning.py", 500), ("near.py", 776), ("far.py", 775)], [],
    )

    assert [(n.file, n.limit) for n in notices] == [("near.py", 800)]


def test_a_run_lists_the_code_band_under_the_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same through a whole run, which passes the band its limits."""
    _code(tmp_path, "at_warning.py", 500)
    _code(tmp_path, "near.py", 790)

    assert file_size.check_file_size(tmp_path, near_cap=True) == 0
    out = capsys.readouterr().out
    assert "near.py: 790/800 lines" in out
    assert "at_warning.py" not in out


def test_the_flags_move_both_numbers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_size, "REPO_ROOT", tmp_path)
    _code(tmp_path, "long.py", 7)
    _code(tmp_path, "over.py", 11)

    argv = ["--warn-code-lines", "5", "--max-code-lines", "10", "--strict"]
    assert file_size.main(argv) == 1
    out = capsys.readouterr().out
    assert "over.py: 11 lines (limit: 10)" in out
    assert "[WARN] 1 code file(s) over 5 lines" in out
    assert "long.py: 7 lines" in out
