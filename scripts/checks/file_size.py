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
from typing import List, NamedTuple, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout, walk_files, DEFAULT_EXCLUDES  # noqa: E402

DEFAULT_MAX_CODE_LINES = 500
DEFAULT_MAX_DOC_WORDS = 3000
DEFAULT_MAX_RFC_WORDS = 8000

_RFC_PREFIX = "docs/rfcs/"

CODE_EXTENSIONS = [".go", ".py", ".rs", ".js", ".ts", ".yaml", ".toml"]
DOC_EXTENSIONS = [".md"]

_EXTRA_EXCLUDES = [
    "**/node_modules/**",
    "cli/target/**",
    "agents/generated/**",
    "internal/generated/**",
]

EXCLUDE_PATTERNS = DEFAULT_EXCLUDES + _EXTRA_EXCLUDES


class FileSizeWarning(NamedTuple):
    file: str
    kind: str
    measured: int
    limit: int
    unit: str


def _count_words(text: str) -> int:
    """Count words in *text*, stripping fenced code blocks."""
    in_code_block = False
    prose_lines: List[str] = []
    for line in text.splitlines():
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
) -> Tuple[List[FileSizeWarning], List[Tuple[str, int]], List[Tuple[str, int]]]:
    """Single-pass scan — returns warnings plus all file measurements."""
    warnings: List[FileSizeWarning] = []
    code_results: List[Tuple[str, int]] = []
    doc_results: List[Tuple[str, int]] = []

    for fpath in walk_files(repo_root, extensions=CODE_EXTENSIONS, exclude_patterns=EXCLUDE_PATTERNS):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line_count = len(text.splitlines())
        rel = fpath.relative_to(repo_root).as_posix()
        code_results.append((rel, line_count))
        if line_count > max_code_lines:
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
        if word_count > effective_limit:
            warnings.append(FileSizeWarning(
                file=rel, kind="doc", measured=word_count,
                limit=effective_limit, unit="words",
            ))

    return warnings, code_results, doc_results


def get_warnings(
    repo_root: Optional[Path] = None,
    max_code_lines: int = DEFAULT_MAX_CODE_LINES,
    max_doc_words: int = DEFAULT_MAX_DOC_WORDS,
) -> List[FileSizeWarning]:
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


def main(argv: Optional[List[str]] = None) -> int:
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
