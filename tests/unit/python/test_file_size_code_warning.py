"""Pin the code-file warning in ``scripts.checks.file_size``.

Until ruling (e) of the sequencing Amendment 2026-09-12, one number did two
jobs: 500 lines was both the size worth splitting and the line a code file
failed the gate at.  That made 500 a cliff, and files piled up against it,
where every fix cost a split or a deleted comment.  The ruling kept 500 as a
warning and moved the failure to 800.

The maintainer's decision of 2026-11-21 moved the warning to 700
(ISSUE-0231).  The check counts every line, and about half the lines of the
Go and Python code are comments, so the warning arrived at about 250 lines
of code and file lengths kept piling up under it.  Counting code lines only
was considered and not taken: it needs a comment parser for five languages,
and a miscount would fail CI.

Pinned here: the numbers, where the warning starts and where the failure
starts, that a file between the old mark and the new one is not warned, that
the warning never changes the exit code, that a failing file is listed once
and printed last, that an allowlisted file is not warned, that both numbers
can be moved by flag and default to the rulings', that the near-cap band for
code sits under the failure and not under the warning, that ``--verbose``
tags each file with the list it is in, that the documentation audit reports
the two lists the same way, and that each listed sentence which states one
of the two marks gives the checker's numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.checks import doc_audit, file_size

# The sentences that state the two marks in the standing texts, each with the
# words that carry them: {warn} is the warning, {limit} the failure, {band}
# the near-cap band under the failure and {pct} that band's share.  The test
# below fails when one of these sentences stops giving the checker's numbers:
# a number moved in the checker and not in the sentence, or the sentence was
# removed, or reworded without its row.
#
# It is a list of what must be there.  It cannot see a statement that is
# added, beside a listed sentence or in a new place, whatever number the new
# statement gives.  So a new sentence that states a mark gets a row here in
# the same change.
#
# Not listed: plans, sequencing documents, RFCs, release records and the
# changelog, which say what held on their day; and this file's own
# docstrings, whose tests fail first when a number moves.
_CHECKER = "scripts/checks/file_size.py"
_SIBLING = "tests/unit/python/test_file_size_checker.py"
_GUIDE = "docs/documentation-guide.md"
_METHOD = "docs/methodology/"
_STATEMENTS_OF_THE_MARKS = [
    (_CHECKER, "- Code files: a warning over {warn} lines, a failure over {limit}"),
    (_CHECKER, "[--warn-code-lines {warn}]"),
    (_CHECKER, "[--max-code-lines {limit}]"),
    (_CHECKER, "The warning was 500 under that ruling and is {warn}"),
    (_CHECKER, "{band} lines of an {limit}-line code file"),
    (_CHECKER, "{band} lines left is {pct}% of a code file's cap"),
    (_CHECKER, "kept 500 as the signal and moved the failure to {limit}"),
    (_CHECKER, "Why {warn}, decided on 2026-11-21"),
    (_CHECKER, "The code cliff has been {limit} lines"),
    (_CHECKER, "so the code band sits under {limit}"),
    (_CHECKER, "past {warn} it gets"),
    (_SIBLING, "three caps ({limit} lines for code,"),
    (_SIBLING, "the warning for code past {warn} lines is pinned"),
    ("scripts/pre_commit.py", "(code: ≤{limit} lines, a warning over {warn}; docs: ≤3000 words)"),
    ("CLAUDE.md", "a code file warns over {warn} lines and fails over {limit}"),
    (_GUIDE, "**A warning over {warn} lines; fails over {limit}**"),
    (_GUIDE, "**A code file over {warn} lines**"),
    (_GUIDE, "is listed as a warning on every run and fails only over {limit}"),
    (_GUIDE, "Never trim it back under {warn}"),
    (_GUIDE, "That ruling made 500 lines the warning and {limit} the failure"),
    (_GUIDE, "**The warning moved from 500 lines to {warn}**"),
    (_GUIDE, "The failure stays at {limit}"),
    (_GUIDE, "for code, the last {band} lines under {limit}"),
    (
        "docs/docs-consistency-checklist.md",
        "(code: ≤{limit} lines, with a warning over {warn}; docs: ≤3000 words)",
    ),
    (_METHOD + "release-cycle.md", "a warning over {warn} lines and fails only over {limit}"),
    (_METHOD + "enforcement-matrix-documents.md", "| Code files ≤ {limit} lines |"),
    (_METHOD + "enforcement-matrix-documents.md", "| A code file over {warn} lines is warned"),
    (
        _METHOD + "automation-catalogue.md",
        "Code ≤ {limit} lines, with a code file over {warn} listed as a warning",
    ),
    (
        _METHOD + "blueprint-extraction.md",
        "The {warn}-line code warning, the {limit}-line code cap",
    ),
    (_METHOD + "review-process.md", "a code file over {warn} lines"),
    (_METHOD + "process-glossary.md", "A code file over {warn} lines is split there"),
]


def _code(root: Path, rel: str, lines: int) -> None:
    """Materialise a code file at *rel* that measures exactly *lines* lines."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines, encoding="utf-8")


def _states(text: str, wanted: str) -> bool:
    """Whether *text* holds *wanted* with no digit against either end, so a
    phrase that ends in 800 is not found in "8000", nor one that starts with
    24 in "124"."""
    return re.search(r"(?<!\d)" + re.escape(wanted) + r"(?!\d)", text) is not None


def test_the_numbers_are_the_rulings() -> None:
    """500 and 800 are ruling (e) of the sequencing Amendment 2026-09-12:
    changing either takes a new amendment, not a PR (decisions, rule 5).
    The warning's move to 700 was made by the maintainer's decision of
    2026-11-21 on ISSUE-0231, ahead of an amendment, and no amendment
    records it yet.  The ruling names lines only; the word limits are pinned
    here so that moving one is a deliberate edit."""
    assert file_size.DEFAULT_WARN_CODE_LINES == 700
    assert file_size.DEFAULT_MAX_CODE_LINES == 800
    assert file_size.DEFAULT_MAX_DOC_WORDS == 3000
    assert file_size.DEFAULT_MAX_RFC_WORDS == 8000


def test_a_number_at_the_end_of_a_phrase_has_an_edge() -> None:
    """A listed phrase that ends or starts with a number must not be found
    inside a longer number."""
    assert _states("it fails over 800 — split it", "fails over 800")
    assert _states("fails over 800", "fails over 800")
    assert not _states("it fails over 8000 — split it", "fails over 800")
    assert not _states("the last 124 lines under 800.", "24 lines under 800")
    assert _states("the last 24 lines under 800.", "24 lines under 800")


@pytest.mark.parametrize(("rel", "phrase"), _STATEMENTS_OF_THE_MARKS)
def test_each_listed_sentence_gives_the_checkers_numbers(rel: str, phrase: str) -> None:
    """The marks are written down in the instructions, the guides and the
    methodology, and a reader meets those before the checker.  Line breaks
    are folded, so re-wrapping a paragraph changes nothing here.  The band
    is the checker's own arithmetic (``margin`` in ``_near_cap_notices``)."""
    limit = file_size.DEFAULT_MAX_CODE_LINES
    pct = file_size.DEFAULT_NEAR_CAP_PCT
    wanted = phrase.format(
        warn=file_size.DEFAULT_WARN_CODE_LINES,
        limit=limit,
        band=max(1, round(limit * pct / 100.0)),
        pct=f"{pct:g}",
    )
    text = " ".join((file_size.REPO_ROOT / rel).read_text(encoding="utf-8").split())

    if not _states(text, wanted):
        raise AssertionError(
            f"{rel} does not say {wanted!r}. If a mark moved, correct the sentence; "
            "if the sentence was reworded, change its row in _STATEMENTS_OF_THE_MARKS."
        )


def test_a_code_file_fails_only_past_800_lines(tmp_path: Path) -> None:
    _code(tmp_path, "past_warning.py", 701)
    _code(tmp_path, "at_limit.py", 800)
    _code(tmp_path, "over.py", 801)

    failures, _, _ = file_size._scan_files(tmp_path)

    assert [(f.file, f.limit) for f in failures] == [("over.py", 800)]


def test_a_code_file_past_700_lines_is_warned_longest_first() -> None:
    """A file exactly at the warning is not past it, and neither is one that
    is only past the mark the warning had before."""
    warned = file_size._code_warnings(
        [("b.py", 750), ("at_warning.py", 700), ("at_limit.py", 800),
         ("a.py", 750), ("past.py", 701), ("past_old_mark.py", 501)],
    )

    assert warned == [
        ("at_limit.py", 800), ("a.py", 750), ("b.py", 750), ("past.py", 701),
    ]


def test_a_file_between_the_old_mark_and_the_new_one_is_not_warned(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What ISSUE-0231 changed, through a whole run with the defaults CI
    uses.  The second run adds one file past the new mark, so "not warned"
    is not a run that prints no warning at all."""
    monkeypatch.setattr(file_size, "REPO_ROOT", tmp_path)
    quiet = ("past_old_mark.py", "mid.py", "at_warning.py")
    _code(tmp_path, "past_old_mark.py", 501)
    _code(tmp_path, "mid.py", 600)
    _code(tmp_path, "at_warning.py", 700)

    assert file_size.main(["--strict"]) == 0
    out = capsys.readouterr().out
    assert "[OK] All files within size limits." in out
    assert "[WARN]" not in out

    _code(tmp_path, "past.py", 701)

    assert file_size.main(["--strict"]) == 0
    out = capsys.readouterr().out
    assert "[WARN] 1 code file(s) over 700 lines (only over 800 fails)" in out
    assert "past.py: 701 lines" in out
    assert [name for name in quiet if name in out] == []


def test_a_failing_file_is_not_warned_as_well() -> None:
    """It is listed once, where it fails, so the output keeps passing and
    failing apart."""
    assert file_size._code_warnings([("over.py", 801)]) == []


def test_an_allowlisted_file_is_not_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist exempts a file from the limit, so the warning skips it,
    as the failure list and the near-cap notice do."""
    monkeypatch.setattr(file_size, "GRANDFATHERED_FILES", frozenset({"waived.py"}))

    assert file_size._code_warnings(
        [("waived.py", 750), ("long.py", 750)],
    ) == [("long.py", 750)]


def test_the_warning_never_changes_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A warning that failed the run would just be the old cliff again."""
    _code(tmp_path, "long.py", 750)

    assert file_size.check_file_size(tmp_path, strict=True) == 0
    out = capsys.readouterr().out
    assert "[OK] All files within size limits." in out
    assert "[WARN] 1 code file(s) over 700 lines" in out
    assert "long.py: 750 lines" in out


def test_a_failing_run_still_prints_the_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing run is when someone is reading this output, and the two
    lists have different tags, so a warning is never read as the failure.
    The failure prints last: the release sweep keeps only the last lines of
    a failing gate."""
    _code(tmp_path, "long.py", 750)
    _code(tmp_path, "over.py", 900)

    assert file_size.check_file_size(tmp_path, strict=True) == 1
    out = capsys.readouterr().out
    assert "[OVER] 1 file(s) exceed size limits:" in out
    assert "over.py: 900 lines (limit: 800)" in out
    assert "[WARN] 1 code file(s) over 700 lines" in out
    assert "long.py: 750 lines" in out
    assert out.index("long.py: 750 lines") < out.index("over.py: 900 lines")


def test_the_code_band_sits_under_the_failure_not_the_warning() -> None:
    """A file at the warning can take many more lines before it fails, so it
    is not near a cap.  One at 776 of 800 is: 3 % of 800 is 24 lines."""
    notices = file_size._near_cap_notices(
        [("at_warning.py", 700), ("near.py", 776), ("far.py", 775)], [],
    )

    assert [(n.file, n.limit) for n in notices] == [("near.py", 800)]


def test_a_run_lists_the_code_band_under_the_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The same through a whole run, which passes the band its limits.  A
    file in the band is warned as well, and neither list offers it a trim."""
    _code(tmp_path, "at_warning.py", 700)
    _code(tmp_path, "near.py", 790)

    assert file_size.check_file_size(tmp_path, near_cap=True) == 0
    out = capsys.readouterr().out
    assert "near.py: 790/800 lines" in out
    assert "near.py: 790 lines" in out
    assert "at_warning.py" not in out
    assert out.count("trim") == out.count("never trim")


def test_verbose_tags_each_file_with_the_list_it_is_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The listing agrees with the lists under it, so a file under the
    warning gets no tag, and neither does an RFC under its own 8 000-word
    limit, however far past 3 000 words it is."""
    _code(tmp_path, "long.py", 750)
    _code(tmp_path, "mid.py", 600)
    _code(tmp_path, "over.py", 900)
    rfc = tmp_path / "docs" / "rfcs" / "0001-r.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text("word " * 3500, encoding="utf-8")

    file_size.check_file_size(tmp_path, verbose=True)
    out = capsys.readouterr().out
    assert "750 lines  long.py [WARN]\n" in out
    assert "600 lines  mid.py\n" in out
    assert "900 lines  over.py [OVER]\n" in out
    assert "3500 words  docs/rfcs/0001-r.md\n" in out


def test_the_command_line_defaults_are_the_rulings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI runs the check with no size flags, so the defaults are what gate."""
    monkeypatch.setattr(file_size, "REPO_ROOT", tmp_path)
    _code(tmp_path, "long.py", 750)
    _code(tmp_path, "over.py", 801)

    assert file_size.main(["--strict"]) == 1
    out = capsys.readouterr().out
    assert "over.py: 801 lines (limit: 800)" in out
    assert "[WARN] 1 code file(s) over 700 lines" in out
    assert "long.py: 750 lines" in out


def test_the_doc_audit_fails_an_over_limit_file_and_warns_a_long_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documentation audit reads the same two lists: a file over its
    limit fails, as it does in CI, a long code file is only a warning, and a
    file under the warning is in neither.  The warning's reason names the
    checker's number."""
    monkeypatch.setattr(doc_audit, "REPO_ROOT", tmp_path)
    _code(tmp_path, "long.py", 750)
    _code(tmp_path, "mid.py", 600)
    _code(tmp_path, "over.py", 900)

    result = doc_audit._run_file_size()

    assert not result.passed
    assert [(v.file, v.detail) for v in result.violations] == [
        ("over.py", "900 lines (limit: 800)"),
    ]
    assert [(w.file, w.detail) for w in result.warnings] == [("long.py", "750 lines")]
    assert result.warnings[0].reason.startswith("code file over 700 lines: ")


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
