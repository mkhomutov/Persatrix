"""Data classes, pattern checking, reporting, and encoding helpers.

Provides the core check-script infrastructure:

- ``Violation`` / ``Pattern`` data classes
- ``check_patterns()`` — bulk regex scanning
- ``report_violations()`` — violation output formatting
- ``ensure_utf8_stdout()`` / ``ensure_utf8_streams()`` — Windows encoding fixes

All utilities use only Python stdlib.  Minimum Python version: 3.11.
"""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from scripts.checks.analysis import has_allow_comment

__all__ = [
    "Pattern",
    "Violation",
    "check_patterns",
    "ensure_utf8_stdout",
    "ensure_utf8_streams",
    "report_violations",
]


# ---------------------------------------------------------------------------
# Range helper
# ---------------------------------------------------------------------------


def _in_ranges(ranges: list[tuple], idx: int) -> bool:
    """Return ``True`` if *idx* falls inside any ``(start, end)`` range."""
    return any(s <= idx <= e for s, e in ranges)


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def ensure_utf8_stdout() -> None:
    """Reconfigure stdout for UTF-8 so emoji prints correctly on Windows."""
    _reconfigure_stream("stdout")


def ensure_utf8_streams() -> None:
    """Reconfigure both stdout and stderr for UTF-8."""
    _reconfigure_stream("stdout")
    _reconfigure_stream("stderr")


def _reconfigure_stream(name: str) -> None:
    stream = getattr(sys, name)
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    elif hasattr(stream, "buffer") and stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
        setattr(
            sys,
            name,
            io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace", line_buffering=True,
            ),
        )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    """A single rule violation found during a check."""

    file: str
    line: int
    message: str
    content: str = ""


@dataclass
class Pattern:
    """A regex pattern used by check_patterns().

    Attributes:
        regex: A Python regex string to search for.
        description: Human-readable explanation shown in violation output.
        prod_only: If True the pattern is only checked in production code.
    """

    regex: str
    description: str
    prod_only: bool = False


# ---------------------------------------------------------------------------
# Bulk pattern checking
# ---------------------------------------------------------------------------


def check_patterns(
    files: Iterable[Path],
    patterns: list[Pattern],
    allow_marker: str | None = None,
    skip_comments: bool = True,
    re_flags: int = 0,
) -> list[Violation]:
    """Apply *patterns* to every line of every file and collect violations.

    Parameters:
        files: Iterable of file paths to scan.
        patterns: List of ``Pattern`` objects.
        allow_marker: If set, lines containing this string are skipped.
        skip_comments: Skip lines starting with comment markers (// or #).
        re_flags: Extra :mod:`re` flags for every pattern.

    Returns:
        A list of ``Violation`` objects.
    """
    compiled = [(re.compile(p.regex, re_flags), p) for p in patterns]

    violations: list[Violation] = []

    for filepath in files:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = text.splitlines()

        for line_idx, raw_line in enumerate(lines):
            line = raw_line

            # Skip comment-only lines when requested.
            stripped = line.lstrip()
            if skip_comments and (stripped.startswith("//") or stripped.startswith("#")):
                continue

            prev_line = lines[line_idx - 1] if line_idx > 0 else ""

            # Check suppression marker.
            if allow_marker and has_allow_comment(line, prev_line, allow_marker):
                continue

            for rx, pat in compiled:
                if not rx.search(line):
                    continue

                violations.append(
                    Violation(
                        file=str(filepath),
                        line=line_idx + 1,  # 1-based
                        message=pat.description,
                        content=line.rstrip(),
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report_violations(
    violations: list[Violation],
    title: str = "Check",
    verbose: bool = False,
    file: TextIO | None = None,
) -> int:
    """Print violations and return an exit code (0 = pass, 1 = fail).

    Parameters:
        violations: The violations to report.
        title: A short name for the check (used in the summary line).
        verbose: If *True*, also print a per-file summary.
        file: Output stream (defaults to ``sys.stdout``).

    Returns:
        ``0`` if *violations* is empty, ``1`` otherwise.
    """
    out = file if file is not None else sys.stdout

    if not violations:
        print("[PASS] {} \u2014 no issues found.".format(title), file=out)
        return 0

    print("[FAIL] Found {} violation(s):\n".format(len(violations)), file=out)
    for v in violations:
        print("  {}:{}".format(v.file, v.line), file=out)
        print("    {}".format(v.message), file=out)
        if v.content:
            print("    > {}".format(v.content), file=out)
        print(file=out)

    if verbose:
        files: dict[str, int] = {}
        for v in violations:
            files[v.file] = files.get(v.file, 0) + 1
        print("  Files with violations:", file=out)
        for fname, count in sorted(files.items()):
            print("    {} ({})".format(fname, count), file=out)
        print(file=out)

    return 1
