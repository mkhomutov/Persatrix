#!/usr/bin/env python3
"""Check for invalid or inconsistent status markers in documentation.

Enforces the standard status marker set across all docs/ markdown files.

Usage::

    python scripts/checks/doc_status_markers.py [--verbose]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402

# Standard allowed markers
ALLOWED_MARKERS = [
    "\u2705 **Implemented**",    # ✅
    "\U0001f680 **Stable**",     # 🚀
    "\U0001f6a7 **In Progress**", # 🚧
    "\u26a0\ufe0f **Partial**",   # ⚠️
    "\U0001f4cb **Planned**",     # 📋
    "\U0001f52e **Future**",      # 🔮
]

CONTEXT_MARKERS = [
    "\U0001f7e2 **Full**",    # 🟢
    "\U0001f7e1 **Partial**", # 🟡
    "\U0001f534 **None**",    # 🔴
]

LEGACY_MARKERS = [
    "\U0001f4dd **Planned**",  # 📝
    "\u274c **Missing**",      # ❌
]

# Common non-standard emojis that may be mistakenly used as status markers.
# Including them here ensures they are detected and flagged rather than
# silently passing because they aren't in the regex at all.
_EXTRA_EMOJIS = (
    r"\U0001f528"  # 🔨 (sometimes used for "Building")
    r"|\u2b50"     # ⭐
    r"|\U0001f6d1" # 🛑
    r"|\U0001f7e0" # 🟠
    r"|\u2615"     # ☕
)

_STATUS_RE = re.compile(
    r"(\u2705|\U0001f680|\U0001f6a7|\u26a0\ufe0f|\U0001f4cb|\U0001f52e"
    r"|\U0001f4dd|\u274c|\U0001f7e2|\U0001f7e1|\U0001f534"
    r"|" + _EXTRA_EMOJIS + r")"
    r"\s*\*\*([^*]+)\*\*"
)

_STATUS_WORDS_RE = re.compile(
    r"Implement|Stable|Progress|Partial|Plan|Future|Missing|Complete",
    re.IGNORECASE,
)


class MarkerIssue(NamedTuple):
    file: str
    line: int
    marker: str
    reason: str


def check_status_markers(
    repo_root: Path,
    verbose: bool = False,
) -> tuple[list[MarkerIssue], list[MarkerIssue]]:
    """Scan docs/ for status markers. Returns (failures, warnings)."""
    docs_dir = repo_root / "docs"
    md_files = sorted(docs_dir.rglob("*.md")) if docs_dir.is_dir() else []

    failures: list[MarkerIssue] = []
    warnings: list[MarkerIssue] = []

    all_allowed = set(ALLOWED_MARKERS)
    all_context = set(CONTEXT_MARKERS)
    all_legacy = set(LEGACY_MARKERS)

    print("[SCAN] Checking documentation status markers...")

    for md_file in md_files:
        rel_path = md_file.relative_to(repo_root).as_posix()

        if verbose:
            print(f"  Checking: {rel_path}")

        try:
            lines = md_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for line_idx, line in enumerate(lines):
            line_num = line_idx + 1
            m = _STATUS_RE.search(line)
            if not m:
                continue

            full_match = m.group(0)
            text = m.group(2)

            is_standard = full_match in all_allowed
            is_context = full_match in all_context
            is_legacy = full_match in all_legacy

            if is_legacy:
                warnings.append(MarkerIssue(
                    file=rel_path,
                    line=line_num,
                    marker=full_match,
                    reason="Legacy marker (should migrate to standard set)",
                ))
            elif not is_standard and not is_context:
                if len(text) > 30:
                    continue
                if _STATUS_WORDS_RE.search(text):
                    allowed_str = ", ".join(ALLOWED_MARKERS)
                    failures.append(MarkerIssue(
                        file=rel_path,
                        line=line_num,
                        marker=full_match,
                        reason=f"Non-standard status marker (use one of: {allowed_str})",
                    ))

    print(f"[OK] Checked status markers in {len(md_files)} markdown file{'s' if len(md_files) != 1 else ''}")
    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(
        description="Check for invalid or inconsistent status markers in docs.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show each file being checked")
    args = parser.parse_args(argv)

    failures, warnings = check_status_markers(REPO_ROOT, verbose=args.verbose)

    if warnings:
        print()
        print(f"[WARN] Found {len(warnings)} legacy marker(s) (consider updating):")
        print()
        for w in warnings:
            print(f"  File:   {w.file}:{w.line}")
            print(f"  Marker: {w.marker}")
            print(f"  Reason: {w.reason}")
            print()

    if failures:
        print()
        print(f"[FAIL] Found {len(failures)} invalid status marker(s):")
        print()
        for f in failures:
            print(f"  File:   {f.file}:{f.line}")
            print(f"  Marker: {f.marker}")
            print(f"  Reason: {f.reason}")
            print()
        return 1

    print("[OK] All status markers are valid!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
