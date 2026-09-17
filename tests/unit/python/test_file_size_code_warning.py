"""Pin the code-file warning in ``scripts.checks.file_size``.

Until ruling (e) of the sequencing Amendment 2026-09-12, one number did two
jobs: 500 lines was both the size worth splitting and the line a code file
failed the gate at.  That made 500 a cliff, and files piled up against it,
where every fix cost a split or a deleted comment.  The ruling keeps 500 as a
warning and moves the failure to 800.

Pinned here: the ruling's numbers, where the warning starts and where the
failure starts, that the warning never changes the exit code, that a failing
file is listed once and printed last, that an allowlisted file is not
warned, that both numbers can be moved by flag and default to the ruling's,
that the near-cap band for code now sits under the failure instead of under
the warning, that ``--verbose`` tags each file with the list it is in, and
that the documentation audit reports the two lists the same way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.checks import doc_audit, file_size


def _code(root: Path, rel: str, lines: int) -> None:
    """Materialise a code file at *rel* that measures exactly *lines* lines."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines, encoding="utf-8")


def test_the_numbers_are_the_rulings() -> None:
    """500 and 800 are ruling (e): changing either takes a new amendment,
    not a PR.  The ruling names lines only; the word limits are pinned here
    so that moving one is a deliberate edit."""
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
        [("b.py", 600), ("at_warning.py", 500), ("at_limit.py", 800),
         ("a.py", 600), ("past.py", 501)],
    )

    assert warned == [
        ("at_limit.py", 800), ("a.py", 600), ("b.py", 600), ("past.py", 501),
    ]


def test_a_failing_file_is_not_warned_as_well() -> None:
    """It is listed once, where it fails, so the output keeps passing and
    failing apart."""
    assert file_size._code_warnings([("over.py", 801)]) == []


def test_an_allowlisted_file_is_not_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist exempts a file from the limit, so the warning skips it,
    as the failure list and the near-cap notice do."""
    monkeypatch.setattr(file_size, "GRANDFATHERED_FILES", frozenset({"waived.py"}))

    assert file_size._code_warnings(
        [("waived.py", 600), ("long.py", 600)],
    ) == [("long.py", 600)]


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
    lists have different tags, so a warning is never read as the failure.
    The failure prints last: the release sweep keeps only the last lines of
    a failing gate."""
    _code(tmp_path, "long.py", 600)
    _code(tmp_path, "over.py", 900)

    assert file_size.check_file_size(tmp_path, strict=True) == 1
    out = capsys.readouterr().out
    assert "[OVER] 1 file(s) exceed size limits:" in out
    assert "over.py: 900 lines (limit: 800)" in out
    assert "[WARN] 1 code file(s) over 500 lines" in out
    assert "long.py: 600 lines" in out
    assert out.index("long.py: 600 lines") < out.index("over.py: 900 lines")


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
    """The same through a whole run, which passes the band its limits.  A
    file in the band is warned as well, and neither list offers it a trim."""
    _code(tmp_path, "at_warning.py", 500)
    _code(tmp_path, "near.py", 790)

    assert file_size.check_file_size(tmp_path, near_cap=True) == 0
    out = capsys.readouterr().out
    assert "near.py: 790/800 lines" in out
    assert "at_warning.py" not in out
    assert out.count("trim") == out.count("never trim")


def test_verbose_tags_each_file_with_the_list_it_is_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The listing agrees with the lists under it, so an RFC under its own
    8 000-word limit gets no tag, however far past 3 000 words it is."""
    _code(tmp_path, "long.py", 600)
    _code(tmp_path, "over.py", 900)
    rfc = tmp_path / "docs" / "rfcs" / "0001-r.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text("word " * 3500, encoding="utf-8")

    file_size.check_file_size(tmp_path, verbose=True)
    out = capsys.readouterr().out
    assert "600 lines  long.py [WARN]\n" in out
    assert "900 lines  over.py [OVER]\n" in out
    assert "3500 words  docs/rfcs/0001-r.md\n" in out


def test_the_command_line_defaults_are_the_rulings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI runs the check with no size flags, so the defaults are what gate."""
    monkeypatch.setattr(file_size, "REPO_ROOT", tmp_path)
    _code(tmp_path, "long.py", 600)
    _code(tmp_path, "over.py", 801)

    assert file_size.main(["--strict"]) == 1
    out = capsys.readouterr().out
    assert "over.py: 801 lines (limit: 800)" in out
    assert "[WARN] 1 code file(s) over 500 lines" in out
    assert "long.py: 600 lines" in out


def test_the_doc_audit_fails_an_over_limit_file_and_warns_a_long_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documentation audit reads the same two lists: a file over its
    limit fails, as it does in CI, and a long code file is only a warning."""
    monkeypatch.setattr(doc_audit, "REPO_ROOT", tmp_path)
    _code(tmp_path, "long.py", 600)
    _code(tmp_path, "over.py", 900)

    result = doc_audit._run_file_size()

    assert not result.passed
    assert [(v.file, v.detail) for v in result.violations] == [
        ("over.py", "900 lines (limit: 800)"),
    ]
    assert [(w.file, w.detail) for w in result.warnings] == [("long.py", "600 lines")]


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
