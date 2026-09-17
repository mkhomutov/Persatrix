#!/usr/bin/env python3
"""File size audit — check code and documentation files against review-friendly limits.

Scans code files and documentation files for excessive size. Files that
exceed the configurable thresholds are flagged so they can be reviewed for
splitting opportunities.

**Thresholds (defaults):**

- Code files: a warning over 500 lines, a failure over 800
- Documentation files: 3 000 words (8 000 under ``docs/rfcs/``)

The limits are a cliff: a file one line under passes and one line over
fails, with no signal in between.  Code gets a warning well before its
cliff (:func:`_code_warnings`), and ``--near-cap`` signals the last few
percent under every limit — see :func:`_near_cap_notices` for why a file
sitting exactly ON a limit is the expensive state, and for the measurement
that motivated it.

Usage::

    python scripts/checks/file_size.py [--warn-code-lines 500]
        [--max-code-lines 800] [--max-doc-words 3000] [--strict] [--verbose]
        [--near-cap [PCT]]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import DEFAULT_EXCLUDES, ensure_utf8_stdout, walk_files  # noqa: E402
from scripts.checks.file_size_allowlist import GRANDFATHERED_FILES  # noqa: E402
from scripts.checks.released import is_released_version_doc, released_versions  # noqa: E402

#: A code file over the warning is listed and never failed; one over the
#: limit fails ``--strict``.  Both numbers are ruling (e) of the sequencing
#: Amendment 2026-09-12 — see :func:`_code_warnings`.
DEFAULT_WARN_CODE_LINES = 500
DEFAULT_MAX_CODE_LINES = 800
DEFAULT_MAX_DOC_WORDS = 3000
DEFAULT_MAX_RFC_WORDS = 8000

#: Default ``--near-cap`` band, as a percentage of each file's own limit —
#: 24 lines of an 800-line code file, 90 words of a 3 000-word doc, 240 of
#: an 8 000-word RFC.  Proportional rather than absolute so one number
#: means the same thing to all three limits.
DEFAULT_NEAR_CAP_PCT = 3.0

_RFC_PREFIX = "docs/rfcs/"

CODE_EXTENSIONS = [".go", ".py", ".rs", ".js", ".ts", ".svelte", ".css", ".yaml", ".toml"]
DOC_EXTENSIONS = [".md"]

_EXTRA_EXCLUDES = [
    "**/node_modules/**",
    "cli/target/**",
    "agents/generated/**",
    "internal/generated/**",
    # Local venvs (not present in CI but common during local runs).
    ".venv/**",
    "venv/**",
    ".notices-venv/**",
    # Generated dependency manifest; word count scales with the Go/Python/Rust
    # dependency graphs. Review it via `make notices` diff, not size limits.
    "THIRD_PARTY_NOTICES.md",
    # Auto-generated issue index (scripts/issues.py); its word count scales with
    # the number of tracked issues, not prose authored in the file. Reviewed via
    # `python scripts/issues.py --check` (sync) + per-issue front-matter, not a
    # prose cap — same data-scaling rationale as THIRD_PARTY_NOTICES.md above.
    "docs/issues/INDEX.md",
    # Generated merged-PR history (scripts/merged_prs.py): one row per squash
    # merge on main, so its length is the repository's PR count, not prose.
    "docs/merged-prs.md",
    # PR review reports are local-only working artifacts and are intentionally
    # not committed; local copies should not block repo-wide size checks.
    "docs/pr-reviews/**",
    # Git worktrees are checked out under .claude/worktrees/ and mirror the
    # full repo tree. Scanning them would double-count every file and report
    # false positives for files that are grandfathered under their normal paths.
    ".claude/**",
    # The grandfather allowlist (scripts/checks/file_size_allowlist.py) is pure
    # reference data — a frozenset of path strings, one per release artifact,
    # each with an inline rationale. Its length scales with release history, not
    # with authored logic, so the *code* line limit would punish it for doing
    # its job. Excluded for the same "size scales with data, not prose" reason
    # as THIRD_PARTY_NOTICES.md and docs/issues/INDEX.md above. This keeps
    # file_size.py itself honestly under the code cap (the logic, not the data).
    "scripts/checks/file_size_allowlist.py",
    # Per-release manual-test execution reports and release checklists. Both are
    # written once against a fixed tag and never edited again: their length is
    # *release evidence* (one row per test, per gate, per upgrade note), not
    # authored prose, and it scales with how much the release shipped. They can
    # never shrink back under the cap, so enumerating them one-by-one in
    # GRANDFATHERED_FILES accumulated a per-release entry carrying a "remove
    # once the tag ships" exit condition that was never achievable — 19 such
    # entries had piled up across v0.3.0–v0.3.10 and none was ever retired.
    # Excluded by pattern for the same "size scales with data, not prose"
    # reason as THIRD_PARTY_NOTICES.md and docs/issues/INDEX.md above, so the
    # allowlist stops growing by one entry per release.
    #
    # Deliberately narrow: this matches only the two write-once categories.
    # Master plans (docs/v*-plan.md) and release-prep plans are NOT matched here
    # because they are *edited during* their cycle and the cap still does useful
    # work on them; they are excluded conditionally instead — once their
    # version has shipped (scripts/checks/released.py). Permanent acceptance gates that happen to
    # live under docs/manual-tests/ (MT-MEMORY-005, MT-CHANNEL-GOV-004) are not
    # per-release reports and are not matched here. The `v[0-9]` prefix keeps
    # non-version names (verify-*, variants-*) out; note fnmatch's `*` crosses
    # `/`, so a nested dir named `docs/v0…/` would match too — none exists, and
    # test_release_evidence_exclusion_stays_narrow pins the rest.
    "docs/manual-tests/v[0-9]*-execution-report.md",
    "docs/v[0-9]*-release-checklist.md",
]

EXCLUDE_PATTERNS = DEFAULT_EXCLUDES + _EXTRA_EXCLUDES

# Version-cycle documents of released versions are frozen release evidence and
# are excluded conditionally (ISSUE-0139) — the predicate and the CHANGELOG read
# live in scripts/checks/released.py, shared with the plan-status checker. The
# names are bound here so tests can monkeypatch ``file_size._released_versions``.
_released_versions = released_versions
_is_released_version_doc = is_released_version_doc


def _stale_allowlist_entries(released: frozenset[str]) -> list[str]:
    """Allowlist entries that are released version-cycle docs.

    Such an entry is dead weight: the file is excluded before the allowlist is
    consulted. It is reported as a notice, not a failure — the entry for the
    open cycle's plan becomes stale the moment the changelog is dated, and
    turning ``main`` red between that and the follow-up that retires it (the
    next plan's first section) would punish every unrelated PR in between.
    """
    return sorted(rel for rel in GRANDFATHERED_FILES if _is_released_version_doc(rel, released))


class FileSizeFailure(NamedTuple):
    """A file over its limit: what ``--strict`` fails on, printed as ``[OVER]``."""

    file: str
    kind: str
    measured: int
    limit: int
    unit: str


class NearCapNotice(NamedTuple):
    """A file that PASSES the gate but has almost no room left."""

    file: str
    kind: str
    measured: int
    limit: int
    unit: str
    headroom: int

    @property
    def headroom_fraction(self) -> float:
        """Headroom as a fraction of this file's own limit.

        The report mixes three units against three caps, so raw headroom
        does not rank them against each other: 24 lines left is 3% of a
        code file's cap — the loosest a notice can be — while 24 words
        left is 0.8% of a doc's, which is nearly out of room.  Sorting on
        the fraction is the same choice the *band* already makes, and for
        the same reason (see :data:`DEFAULT_NEAR_CAP_PCT`); ranking on the
        raw number instead put every code file above every doc that was
        proportionally tighter.
        """
        return self.headroom / self.limit if self.limit else 0.0


def _count_words(text: str) -> int:
    """Count words in *text*, stripping fenced code blocks and YAML front-matter.

    YAML front-matter (the ``---``-delimited block at the very top of a doc)
    is structured metadata, not prose — counting it against the word cap
    would punish RFC/issue files for adding required machine-readable
    metadata.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    in_code_block = False
    prose_lines: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if not in_code_block:
            prose_lines.append(line)
    return len(" ".join(prose_lines).split())


def _scan_files(
    repo_root: Path,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
    max_rfc_words: int = DEFAULT_MAX_RFC_WORDS,
) -> tuple[list[FileSizeFailure], list[tuple[str, int]], list[tuple[str, int]]]:
    """Single-pass scan — returns the failures plus all file measurements."""
    failures: list[FileSizeFailure] = []
    code_results: list[tuple[str, int]] = []
    doc_results: list[tuple[str, int]] = []
    released = _released_versions(repo_root)

    for fpath in walk_files(
        repo_root, extensions=CODE_EXTENSIONS, exclude_patterns=EXCLUDE_PATTERNS,
    ):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line_count = len(text.splitlines())
        rel = fpath.relative_to(repo_root).as_posix()
        code_results.append((rel, line_count))
        if line_count > max_code_lines and rel not in GRANDFATHERED_FILES:
            failures.append(FileSizeFailure(
                file=rel, kind="code", measured=line_count,
                limit=max_code_lines, unit="lines",
            ))

    for fpath in walk_files(
        repo_root, extensions=DOC_EXTENSIONS, exclude_patterns=EXCLUDE_PATTERNS,
    ):
        rel = fpath.relative_to(repo_root).as_posix()
        if _is_released_version_doc(rel, released):
            continue  # frozen release evidence — see _VERSION_DOC_RE
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        word_count = _count_words(text)
        doc_results.append((rel, word_count))
        effective_limit = max_rfc_words if rel.startswith(_RFC_PREFIX) else max_doc_words
        if word_count > effective_limit and rel not in GRANDFATHERED_FILES:
            failures.append(FileSizeFailure(
                file=rel, kind="doc", measured=word_count,
                limit=effective_limit, unit="words",
            ))

    return failures, code_results, doc_results


def _code_warnings(
    code_results: list[tuple[str, int]],
    *,
    warn_code_lines: int = DEFAULT_WARN_CODE_LINES,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
) -> list[tuple[str, int]]:
    """Code files over the warning but not over the limit, longest first.

    Why a warning and not the gate.  500 lines used to be both the size
    worth splitting and the limit, so files piled up on it (see
    :func:`_near_cap_notices`), and a pre-release sweep that split them
    by line count only refilled the pile.  Ruling (e) of the sequencing
    Amendment 2026-09-12 keeps 500 as the signal and moves the failure to
    800: a file listed here is split at a real seam when a change edits it
    for another reason — never trimmed to fit, never swept.

    A file over the limit is left out: it already fails, and listing it
    twice would blur passing and failing.  An allowlisted file is left out
    too, as in the other tiers.  Never affects the exit code.
    """
    warned = [
        (rel, lines) for rel, lines in code_results
        if warn_code_lines < lines <= max_code_lines and rel not in GRANDFATHERED_FILES
    ]
    return sorted(warned, key=lambda item: (-item[1], item[0]))


def _near_cap_notices(
    code_results: list[tuple[str, int]],
    doc_results: list[tuple[str, int]],
    *,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
    max_rfc_words: int = DEFAULT_MAX_RFC_WORDS,
    pct: float = DEFAULT_NEAR_CAP_PCT,
) -> list[NearCapNotice]:
    """Files within ``pct`` of their limit but not yet over it.

    Why this exists.  The limits are a cliff, so the state just below one
    is easy to miss — and it is also the expensive one.  A file sitting
    exactly ON the limit cannot take a one-line fix: the next change to it
    is either a split, or a trim that deletes existing rationale to make
    room, and the trim is the tempting option because it is local.  That
    silently converts "add a guard" into "delete the comment explaining
    the last guard".

    This is measured, not theorised.  A sweep on 2026-08-29 found **29**
    non-waived code files at exactly 500 lines, against a mean of 3.55 per
    line-count bucket over the surrounding 480-499 range (71 files over 20
    buckets) — 8.2x the local density, with a cliff to zero at 501.  The
    mean is carried to two decimals so the ratio re-derives: 29/3.5 would
    give 8.3, not the 8.2 measured.  File sizes do not naturally
    pile up on a round number; that shape is what trimming-to-fit leaves
    behind.  Warning before the cliff turns the surprise into notice, and
    changes nothing about what blocks CI: these are notices, never
    failures, and they never affect the exit code.

    The code cliff has been 800 lines since ruling (e) of the sequencing
    Amendment 2026-09-12, so the code band sits under 800.  A file at 500
    can take a one-line fix now; past 500 it gets :func:`_code_warnings`.

    Grandfathered files are skipped — they are exempt from the limit, so
    "approaching" it does not apply to them.
    """
    def margin(cap: int) -> int:
        # At least 1, so a tiny cap (a test override) still has a band.
        return max(1, round(cap * pct / 100.0))

    notices: list[NearCapNotice] = []
    for rel, measured in code_results:
        if rel in GRANDFATHERED_FILES:
            continue
        headroom = max_code_lines - measured
        if 0 <= headroom <= margin(max_code_lines):
            notices.append(NearCapNotice(
                rel, "code", measured, max_code_lines, "lines", headroom,
            ))
    for rel, measured in doc_results:
        if rel in GRANDFATHERED_FILES:
            continue
        cap = max_rfc_words if rel.startswith(_RFC_PREFIX) else max_doc_words
        headroom = cap - measured
        if 0 <= headroom <= margin(cap):
            notices.append(NearCapNotice(
                rel, "doc", measured, cap, "words", headroom,
            ))
    notices.sort(key=lambda n: (n.headroom_fraction, n.file))
    return notices


def get_failures_and_warnings(
    repo_root: Path | None = None,
) -> tuple[list[FileSizeFailure], list[tuple[str, int]]]:
    """Programmatic API — the ``[OVER]`` and ``[WARN]`` lists, without printing."""
    failures, code_results, _ = _scan_files(repo_root or REPO_ROOT)
    return failures, _code_warnings(code_results)


def check_file_size(
    repo_root: Path,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
    strict: bool = False,
    verbose: bool = False,
    near_cap: bool = False,
    near_cap_pct: float = DEFAULT_NEAR_CAP_PCT,
    warn_code_lines: int = DEFAULT_WARN_CODE_LINES,
) -> int:
    """Run the file size audit. Returns 0/1 depending on findings and mode.

    Code files over ``warn_code_lines`` but within their limit are always
    listed in full as a warning (:func:`_code_warnings`).

    ``near_cap`` lists the files approaching their limit
    (:func:`_near_cap_notices`); without it their COUNT is still reported
    on one line, so the tier is discoverable from ordinary output instead
    of only from ``--help``.  Neither the warning nor this tier affects the
    exit code — a file that is merely long or close is passing, and making
    it fail would just move the cliff.

    Both print on every run, the failing ``--strict`` ones included: CI
    runs ``--strict``, so returning early on a failure would have hidden
    them from the one audience already reading size output.
    """
    failures, code_results, doc_results = _scan_files(repo_root, max_code_lines, max_doc_words)
    long_code = _code_warnings(
        code_results, warn_code_lines=warn_code_lines, max_code_lines=max_code_lines,
    )
    for rel in _stale_allowlist_entries(_released_versions(repo_root)):
        print(
            f"[STALE-ALLOWLIST] {rel} is a released version-cycle doc, already excluded — "
            "drop its entry from scripts/checks/file_size_allowlist.py (the next plan's follow-up)."
        )

    print(f"[SCAN] Scanned {len(code_results)} code files and {len(doc_results)} doc files")

    if verbose:
        tags = {rel: " [WARN]" for rel, _ in long_code} | {f.file: " [OVER]" for f in failures}
        print("\n--- Code files ---")
        for rel, lines in sorted(code_results, key=lambda x: -x[1])[:20]:
            print(f"  {lines:>5} lines  {rel}{tags.get(rel, '')}")
        print("\n--- Doc files ---")
        for rel, words in sorted(doc_results, key=lambda x: -x[1])[:20]:
            print(f"  {words:>5} words  {rel}{tags.get(rel, '')}")

    if not failures:
        print("[OK] All files within size limits.")
    if long_code:
        print(
            f"\n[WARN] {len(long_code)} code file(s) over {warn_code_lines} lines "
            f"(only over {max_code_lines} fails) — split one at a real seam when a "
            "change edits it for another reason; never trim it to fit:",
        )
        for rel, lines in long_code:
            print(f"  {rel}: {lines} lines")
    # The failures print after the warning, so a reader that keeps only the
    # end of a failing run (the release sweep keeps 15 lines) still sees them.
    if failures:
        print(f"\n[OVER] {len(failures)} file(s) exceed size limits:")
        for f in failures:
            print(f"  {f.file}: {f.measured} {f.unit} (limit: {f.limit})")

    notices = _near_cap_notices(
        code_results, doc_results,
        max_code_lines=max_code_lines, max_doc_words=max_doc_words,
        pct=near_cap_pct,
    )
    if near_cap and not notices:
        # An explicit request always gets an answer.  Silence on the success
        # path is indistinguishable from a mistyped flag or one that did
        # nothing, which would make the good news unreadable as good news.
        print(f"\n[NEAR] No files within {near_cap_pct:g}% of their limit.")
    elif near_cap:
        print(
            f"\n[NEAR] {len(notices)} file(s) within {near_cap_pct:g}% of "
            f"their limit — little room is left: split one at a real seam, "
            f"never trim it to fit:",
        )
        for n in notices:
            room = "AT THE LIMIT" if n.headroom == 0 else f"{n.headroom} {n.unit} left"
            print(f"  {n.file}: {n.measured}/{n.limit} {n.unit} — {room}")
    elif notices:
        # Deliberately asymmetric: unasked-for, this line is pure
        # discoverability, so it earns its place only when there is
        # something to discover.  A clean run says [OK] and stops.
        at_limit = sum(1 for n in notices if n.headroom == 0)
        print(
            f"[NEAR] {len(notices)} file(s) within {near_cap_pct:g}% of their "
            f"limit ({at_limit} exactly AT it) — run with --near-cap to list.",
        )

    # Decided last, so the tiers above print on every run — including the
    # failing ``--strict`` runs, which is when someone is already reading
    # this output.  Only an over-cap file can fail the gate.
    return 1 if failures and strict else 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="Check file sizes against review-friendly limits.")
    parser.add_argument(
        "--warn-code-lines", type=int, default=DEFAULT_WARN_CODE_LINES,
        help="List code files over this many lines. Never changes the exit code.",
    )
    parser.add_argument(
        "--max-code-lines", type=int, default=DEFAULT_MAX_CODE_LINES,
        help="The code limit: a file over this many lines fails --strict.",
    )
    parser.add_argument("--max-doc-words", type=int, default=DEFAULT_MAX_DOC_WORDS)
    parser.add_argument("--strict", action="store_true", help="Exit 1 if a file is over its limit")
    parser.add_argument("--verbose", action="store_true", help="Show all scanned files")
    parser.add_argument(
        "--near-cap", nargs="?", type=float, const=DEFAULT_NEAR_CAP_PCT,
        default=None, metavar="PCT",
        help=(
            "List files within PCT%% of their limit (default "
            f"{DEFAULT_NEAR_CAP_PCT:g}%%). Never changes the exit code."
        ),
    )
    args = parser.parse_args(argv)

    return check_file_size(
        REPO_ROOT,
        max_code_lines=args.max_code_lines,
        max_doc_words=args.max_doc_words,
        strict=args.strict,
        verbose=args.verbose,
        near_cap=args.near_cap is not None,
        near_cap_pct=(
            args.near_cap if args.near_cap is not None else DEFAULT_NEAR_CAP_PCT
        ),
        warn_code_lines=args.warn_code_lines,
    )


if __name__ == "__main__":
    sys.exit(main())
