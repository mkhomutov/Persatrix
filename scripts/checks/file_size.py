#!/usr/bin/env python3
"""File size audit — check code and documentation files against review-friendly limits.

Scans code files and documentation files for excessive size. Files that
exceed the configurable thresholds are flagged so they can be reviewed for
splitting opportunities.

**Thresholds (defaults):**

- Code files: 500 lines
- Documentation files: 3 000 words

Usage::

    python scripts/checks/file_size.py [--max-code-lines 500] [--max-doc-words 3000] [--strict] [--verbose]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout, walk_files, DEFAULT_EXCLUDES  # noqa: E402
from scripts.checks.file_size_allowlist import GRANDFATHERED_FILES  # noqa: E402

DEFAULT_MAX_CODE_LINES = 500
DEFAULT_MAX_DOC_WORDS = 3000
DEFAULT_MAX_RFC_WORDS = 8000

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
    # with authored logic, so the 500-line *code* cap would punish it for doing
    # its job. Excluded for the same "size scales with data, not prose" reason
    # as THIRD_PARTY_NOTICES.md and docs/issues/INDEX.md above. This keeps
    # file_size.py itself honestly under the code cap (the logic, not the data).
    "scripts/checks/file_size_allowlist.py",
]

EXCLUDE_PATTERNS = DEFAULT_EXCLUDES + _EXTRA_EXCLUDES

# ``GRANDFATHERED_FILES`` — the size-audit allowlist — lives in
# ``scripts/checks/file_size_allowlist.py`` (imported above). It is reference
# data whose length scales with release history, so it is kept out of this
# module to keep the *logic* honestly under the 500-line code cap; see that
# module's docstring for the full rationale.


class FileSizeWarning(NamedTuple):
    file: str
    kind: str
    measured: int
    limit: int
    unit: str


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
) -> tuple[list[FileSizeWarning], list[tuple[str, int]], list[tuple[str, int]]]:
    """Single-pass scan — returns warnings plus all file measurements."""
    warnings: list[FileSizeWarning] = []
    code_results: list[tuple[str, int]] = []
    doc_results: list[tuple[str, int]] = []

    for fpath in walk_files(repo_root, extensions=CODE_EXTENSIONS, exclude_patterns=EXCLUDE_PATTERNS):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line_count = len(text.splitlines())
        rel = fpath.relative_to(repo_root).as_posix()
        code_results.append((rel, line_count))
        if line_count > max_code_lines and rel not in GRANDFATHERED_FILES:
            warnings.append(FileSizeWarning(
                file=rel, kind="code", measured=line_count,
                limit=max_code_lines, unit="lines",
            ))

    for fpath in walk_files(repo_root, extensions=DOC_EXTENSIONS, exclude_patterns=EXCLUDE_PATTERNS):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        word_count = _count_words(text)
        rel = fpath.relative_to(repo_root).as_posix()
        doc_results.append((rel, word_count))
        effective_limit = max_rfc_words if rel.startswith(_RFC_PREFIX) else max_doc_words
        if word_count > effective_limit and rel not in GRANDFATHERED_FILES:
            warnings.append(FileSizeWarning(
                file=rel, kind="doc", measured=word_count,
                limit=effective_limit, unit="words",
            ))

    return warnings, code_results, doc_results


def get_warnings(
    repo_root: Path | None = None,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
) -> list[FileSizeWarning]:
    """Programmatic API — returns warnings without printing."""
    root = repo_root or REPO_ROOT
    warnings, _, _ = _scan_files(root, max_code_lines, max_doc_words)
    return warnings


def check_file_size(
    repo_root: Path,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
    strict: bool = False,
    verbose: bool = False,
) -> int:
    """Run the file size audit. Returns 0/1 depending on findings and mode."""
    warnings, code_results, doc_results = _scan_files(repo_root, max_code_lines, max_doc_words)

    print(f"[SCAN] Scanned {len(code_results)} code files and {len(doc_results)} doc files")

    if verbose:
        print("\n--- Code files ---")
        for rel, lines in sorted(code_results, key=lambda x: -x[1])[:20]:
            flag = " ⚠" if lines > max_code_lines else ""
            print(f"  {lines:>5} lines  {rel}{flag}")
        print("\n--- Doc files ---")
        for rel, words in sorted(doc_results, key=lambda x: -x[1])[:20]:
            flag = " ⚠" if words > max_doc_words else ""
            print(f"  {words:>5} words  {rel}{flag}")

    if warnings:
        print(f"\n[WARN] {len(warnings)} file(s) exceed size limits:")
        for w in warnings:
            print(f"  {w.file}: {w.measured} {w.unit} (limit: {w.limit})")

        if strict:
            return 1
    else:
        print("[OK] All files within size limits.")

    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="Check file sizes against review-friendly limits.")
    parser.add_argument("--max-code-lines", type=int, default=DEFAULT_MAX_CODE_LINES)
    parser.add_argument("--max-doc-words", type=int, default=DEFAULT_MAX_DOC_WORDS)
    parser.add_argument("--strict", action="store_true", help="Exit 1 on warnings")
    parser.add_argument("--verbose", action="store_true", help="Show all scanned files")
    args = parser.parse_args(argv)

    return check_file_size(
        REPO_ROOT,
        max_code_lines=args.max_code_lines,
        max_doc_words=args.max_doc_words,
        strict=args.strict,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
