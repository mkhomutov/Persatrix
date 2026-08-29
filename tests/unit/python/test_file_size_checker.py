"""Pin the contract of ``scripts.checks.file_size`` — the repo size gate.

The checker enforces three caps (500 lines for code, 3 000 words for docs,
8 000 for ``docs/rfcs/``) with two escape hatches that are easy to get
subtly wrong, and until now had no test:

    * ``GRANDFATHERED_FILES`` — an *allowlist*: the file is still scanned
      and measured, it just does not raise. Entries are expected to go
      away once the file shrinks or is archived.
    * ``EXCLUDE_PATTERNS`` — an *exclusion*: the file is never scanned at
      all. Reserved for content whose size scales with data rather than
      authored prose.

The distinction matters. A file moved from the allowlist to the excludes
stops being measured entirely, so a regression in it becomes invisible —
which is exactly why the 2026-07-25 cleanup narrowed the new
release-evidence patterns to two write-once categories instead of globbing
every ``docs/v*`` artifact. These tests pin that narrowness, so a future
broadening (e.g. ``docs/v*-plan.md``) fails here rather than silently
removing the cap from a doc that is still being edited.

Also pinned: the measurement contract (fenced code and YAML front-matter
do not count toward the prose cap — the reason a doc's raw ``wc -w`` and
its gate measurement legitimately differ), the RFC-vs-doc limit split, and
two hygiene invariants on the allowlist itself.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import pytest

from scripts.checks import file_size
from scripts.checks.file_size import (
    DEFAULT_MAX_DOC_WORDS,
    DEFAULT_MAX_RFC_WORDS,
    EXCLUDE_PATTERNS,
    _count_words,
    _near_cap_notices,
    _scan_files,
)
from scripts.checks.file_size_allowlist import GRANDFATHERED_FILES

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write(root: Path, rel: str, words: int) -> Path:
    """Materialise a doc at *rel* with *words* words of countable prose."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(" ".join(["word"] * words) + "\n", encoding="utf-8")
    return path


def _measured(root: Path) -> dict[str, int]:
    """Map of relative path -> measured word count for every *scanned* doc."""
    _, _, doc_results = _scan_files(root)
    return dict(doc_results)


# --------------------------------------------------------------------------
# Measurement contract
# --------------------------------------------------------------------------


def test_count_words_ignores_fenced_code_blocks() -> None:
    """Fenced code does not count toward the prose cap.

    This is why a plan doc can measure under the cap while ``wc -w``
    reports more: dependency graphs and config samples are fenced.
    """
    prose_only = _count_words("alpha beta gamma\n")
    with_fence = _count_words(
        "alpha beta gamma\n"
        "```\n"
        + " ".join(["noise"] * 500)
        + "\n```\n"
    )
    assert prose_only == 3
    assert with_fence == 3


def test_count_words_ignores_tilde_fences() -> None:
    """``~~~`` fences are stripped as well as backtick fences."""
    assert _count_words("alpha\n~~~\n" + " ".join(["noise"] * 100) + "\n~~~\n") == 1


def test_count_words_ignores_yaml_front_matter() -> None:
    """RFC/issue front-matter is machine-readable metadata, not prose."""
    text = "---\nid: RFC-0037\ntitle: A Fairly Long Title Here\n---\nalpha beta\n"
    assert _count_words(text) == 2


def test_count_words_counts_ordinary_prose() -> None:
    """Sanity anchor: plain prose is counted verbatim."""
    assert _count_words("one two three four five\n") == 5


# --------------------------------------------------------------------------
# Cap selection
# --------------------------------------------------------------------------


def test_rfc_docs_get_the_higher_word_limit(tmp_path: Path) -> None:
    """``docs/rfcs/`` measures against 8 000 words; other docs against 3 000."""
    _write(tmp_path, "docs/rfcs/0099-example.md", DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/guides/example.md", DEFAULT_MAX_DOC_WORDS + 50)

    warnings, _, _ = _scan_files(tmp_path)
    flagged = {w.file: w for w in warnings}

    assert "docs/guides/example.md" in flagged
    assert flagged["docs/guides/example.md"].limit == DEFAULT_MAX_DOC_WORDS
    # Same word count, but comfortably under the RFC limit.
    assert "docs/rfcs/0099-example.md" not in flagged


def test_rfc_docs_still_flag_above_the_rfc_limit(tmp_path: Path) -> None:
    """The RFC limit is a higher cap, not the absence of one."""
    _write(tmp_path, "docs/rfcs/0099-example.md", DEFAULT_MAX_RFC_WORDS + 50)

    warnings, _, _ = _scan_files(tmp_path)
    flagged = {w.file: w for w in warnings}

    assert "docs/rfcs/0099-example.md" in flagged
    assert flagged["docs/rfcs/0099-example.md"].limit == DEFAULT_MAX_RFC_WORDS


# --------------------------------------------------------------------------
# Allowlist vs exclusion — the distinction that matters
# --------------------------------------------------------------------------


def test_allowlisted_file_is_measured_but_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowlisted doc still gets measured — it is silenced, not skipped.

    This is the property that separates the allowlist from the excludes:
    the measurement stays available (``--verbose`` reports it, and a
    future maintainer can see whether it has shrunk back under the cap).
    """
    rel = "docs/some-long-plan.md"
    _write(tmp_path, rel, DEFAULT_MAX_DOC_WORDS + 50)

    monkeypatch.setattr(file_size, "GRANDFATHERED_FILES", frozenset({rel}))
    warnings, _, doc_results = _scan_files(tmp_path)

    assert rel not in {w.file for w in warnings}, "allowlisted file must not raise"
    assert rel in dict(doc_results), "allowlisted file must still be measured"


def test_release_evidence_is_excluded_from_the_scan_entirely(tmp_path: Path) -> None:
    """Execution reports and release checklists are never scanned.

    Write-once release evidence: frozen against a tag, can never shrink,
    so it is excluded by pattern rather than accumulating one allowlist
    entry per release. Uses an invented future version to prove the
    pattern generalises rather than matching a hard-coded list.
    """
    _write(tmp_path, "docs/manual-tests/v9.9.9-execution-report.md",
           DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/v9.9.9-release-checklist.md",
           DEFAULT_MAX_DOC_WORDS + 50)

    measured = _measured(tmp_path)

    assert "docs/manual-tests/v9.9.9-execution-report.md" not in measured
    assert "docs/v9.9.9-release-checklist.md" not in measured


def test_release_evidence_exclusion_stays_narrow(tmp_path: Path) -> None:
    """The exclusion must not swallow docs that are still edited.

    Two neighbours are deliberately *not* excluded:

    * ``docs/manual-tests/MT-*.md`` — permanent acceptance gates that
      happen to share the directory with per-release reports.
    * ``docs/v*-plan.md`` — master plans, which are edited throughout
      their release cycle and where the cap still does useful work.
    * Non-version names that merely start with ``v`` (``verify-*``,
      ``variants-*``) — the patterns require a digit after the ``v``
      (fnmatch ``v[0-9]``), so loosening that back to ``v*`` fails here.

    Broadening either pattern silently removes the cap from a live
    document; this test is the tripwire for that.
    """
    _write(tmp_path, "docs/manual-tests/MT-EXAMPLE-001.md",
           DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/v9.9.9-plan.md", DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/v9.9.9-release-prep-plan.md",
           DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/manual-tests/verify-cache-execution-report.md",
           DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/variants-release-checklist.md",
           DEFAULT_MAX_DOC_WORDS + 50)

    measured = _measured(tmp_path)
    flagged = {w.file for w in _scan_files(tmp_path)[0]}

    for rel in (
        "docs/manual-tests/MT-EXAMPLE-001.md",
        "docs/v9.9.9-plan.md",
        "docs/v9.9.9-release-prep-plan.md",
        "docs/manual-tests/verify-cache-execution-report.md",
        "docs/variants-release-checklist.md",
    ):
        assert rel in measured, f"{rel} must still be scanned"
        assert rel in flagged, f"{rel} must still be capped"


# --------------------------------------------------------------------------
# Allowlist hygiene (live repo)
# --------------------------------------------------------------------------


def test_allowlist_has_no_dead_entries() -> None:
    """Every allowlisted path exists.

    A stale entry outlives the file it silenced (deleted, renamed, or
    split), and quietly grants an exemption to nothing.
    """
    missing = sorted(rel for rel in GRANDFATHERED_FILES if not (REPO_ROOT / rel).exists())
    assert not missing, (
        "allowlist references files that no longer exist — remove these entries "
        f"from scripts/checks/file_size_allowlist.py: {missing}"
    )


def test_allowlist_entries_are_not_already_excluded() -> None:
    """No allowlist entry duplicates an exclude pattern.

    An excluded file is never scanned, so allowlisting it too is dead
    weight. This is what stops per-release execution reports and release
    checklists from creeping back into the allowlist after the 2026-07-25
    cleanup moved them to ``EXCLUDE_PATTERNS``.
    """
    redundant = sorted(
        rel for rel in GRANDFATHERED_FILES
        if any(fnmatch(rel, pat.replace("\\", "/")) for pat in EXCLUDE_PATTERNS)
    )
    assert not redundant, (
        "these allowlist entries are already covered by EXCLUDE_PATTERNS in "
        f"scripts/checks/file_size.py — drop them from the allowlist: {redundant}"
    )


def test_repo_currently_passes_its_own_size_gate() -> None:
    """The live tree is clean — the gate and the allowlist agree today.

    Guards the pair: adding a pattern without pruning the allowlist, or
    pruning the allowlist without adding the pattern, both surface here.
    """
    warnings, _, _ = _scan_files(REPO_ROOT)
    assert not warnings, (
        "repo exceeds its size gate: "
        + ", ".join(f"{w.file} ({w.measured} {w.unit} > {w.limit})" for w in warnings)
    )


# --------------------------------------------------------------------------
# The near-cap tier — advance notice, never a gate
# --------------------------------------------------------------------------


def _notice_files(**kw: object) -> set[str]:
    """Near-cap notices for a synthetic measurement set."""
    code = kw.pop("code", [])
    docs = kw.pop("docs", [])
    return {n.file for n in _near_cap_notices(code, docs, **kw)}  # type: ignore[arg-type]


def test_a_file_exactly_at_the_limit_is_flagged() -> None:
    """The state the tier exists for.  A file ON the limit passes the gate
    silently, and is also the one where the next edit costs a split or a
    rationale-deleting trim — 28 code files were sitting here when the
    tier was added."""
    notices = _near_cap_notices([("a.py", 500)], [])

    assert [n.file for n in notices] == ["a.py"]
    assert notices[0].headroom == 0


def test_a_file_over_the_limit_is_not_a_near_cap_notice() -> None:
    """It is a WARNING, and reporting it twice would blur the one
    distinction the output has to keep: passing versus failing."""
    assert _notice_files(code=[("a.py", 501)]) == set()


def test_the_band_is_proportional_to_each_limit() -> None:
    """One percentage means the same thing to all three caps: 3% is 15
    lines of a code file, 90 words of a doc, 240 of an RFC."""
    assert _notice_files(code=[("near.py", 485), ("far.py", 484)]) == {"near.py"}
    assert _notice_files(
        docs=[("docs/d.md", 2910), ("docs/far.md", 2909)],
    ) == {"docs/d.md"}
    assert _notice_files(
        docs=[("docs/rfcs/0099-r.md", 7760), ("docs/rfcs/0098-r.md", 7759)],
    ) == {"docs/rfcs/0099-r.md"}


def test_a_tighter_band_narrows_the_report() -> None:
    assert _notice_files(code=[("a.py", 490)], pct=3) == {"a.py"}
    assert _notice_files(code=[("a.py", 490)], pct=1) == set()


def test_allowlisted_files_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """They are exempt from the limit, so "approaching" it is meaningless
    — and they are mostly already far past it."""
    monkeypatch.setattr(file_size, "GRANDFATHERED_FILES", frozenset({"waived.py"}))

    assert _notice_files(code=[("waived.py", 500), ("live.py", 500)]) == {"live.py"}


def test_notices_sort_tightest_first() -> None:
    notices = _near_cap_notices(
        [("roomy.py", 490), ("at.py", 500), ("tight.py", 499)], [],
    )

    assert [n.file for n in notices] == ["at.py", "tight.py", "roomy.py"]


def test_near_cap_never_changes_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The contract that keeps this a signal rather than a second cliff:
    a file that is merely close is passing, and failing it would just move
    the cliff down by 3%."""
    _write(tmp_path, "docs/close.md", DEFAULT_MAX_DOC_WORDS - 5)

    assert file_size.check_file_size(tmp_path, strict=True, near_cap=True) == 0
    out = capsys.readouterr().out
    assert "docs/close.md" in out
    assert "[OK] All files within size limits." in out


def test_the_count_is_reported_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Discoverable from ordinary output, not just from ``--help`` — but
    one line, so it cannot drown the warnings that do gate."""
    _write(tmp_path, "docs/close.md", DEFAULT_MAX_DOC_WORDS)

    file_size.check_file_size(tmp_path, strict=True)
    out = capsys.readouterr().out

    assert "1 exactly AT it" in out
    assert "--near-cap" in out
    assert "docs/close.md" not in out, "the list belongs behind the flag"
