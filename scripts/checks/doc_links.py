#!/usr/bin/env python3
"""Check for broken internal documentation links.

Validates that markdown links in docs/ and root-level .md files point to
existing files.

Usage::

    python scripts/checks/doc_links.py [--verbose]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

# Repo root: three levels up from this file (scripts/checks/doc_links.py).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402

# Markdown link pattern: [text](path) or [text](path#anchor)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)#]*)(#[^)]+)?\)")


class BrokenLink(NamedTuple):
    file: str
    link: str
    target: str
    reason: str


def _collect_md_files(repo_root: Path) -> list[Path]:
    """Return all markdown files in docs/ (recursive) and repo root (depth <=1)."""
    docs_dir = repo_root / "docs"
    files: list[Path] = []

    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.rglob("*.md")))

    root_files = set(repo_root.glob("*.md"))
    root_files.update(repo_root.glob("*/*.md"))
    if docs_dir.is_dir():
        docs_resolved = os.path.normcase(str(docs_dir.resolve()))
        root_files = {
            f for f in root_files
            if not os.path.normcase(str(f.resolve())).startswith(
                docs_resolved + os.sep
            )
            and os.path.normcase(str(f.resolve())) != docs_resolved
        }
    files.extend(sorted(root_files))

    return files


def check_doc_links(repo_root: Path, verbose: bool = False) -> list[BrokenLink]:
    """Scan markdown files and return a list of broken internal links."""
    md_files = _collect_md_files(repo_root)
    failures: list[BrokenLink] = []
    checked = 0

    print("[SCAN] Checking documentation links...")

    for md_file in md_files:
        rel_path = md_file.relative_to(repo_root).as_posix()

        if verbose:
            print(f"  Checking: {rel_path}")

        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if not content.strip():
            continue

        file_dir = md_file.parent

        for match in _LINK_RE.finditer(content):
            link_path = match.group(2)
            anchor = match.group(3) or ""
            full_link = link_path + anchor

            if re.match(r"^https?://", link_path) or link_path.startswith("mailto:"):
                continue
            if re.match(r"^[a-z]+://", link_path):
                continue
            if not link_path.strip() and anchor:
                continue
            if not link_path.strip():
                continue

            checked += 1

            if link_path.startswith("/"):
                target = repo_root / link_path.lstrip("/")
            else:
                target = file_dir / link_path

            try:
                target = target.resolve()
            except (OSError, ValueError):
                failures.append(BrokenLink(
                    file=rel_path,
                    link=full_link,
                    target=link_path,
                    reason="Invalid path characters in link target",
                ))
                continue

            if not target.is_file() and not target.is_dir():
                failures.append(BrokenLink(
                    file=rel_path,
                    link=full_link,
                    target=link_path,
                    reason=f"Target not found: {target}",
                ))

    _n = len(md_files)
    print(f"[OK] Checked {checked} links in {_n} markdown file{'s' if _n != 1 else ''}")
    return failures


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="Check for broken documentation links.")
    parser.add_argument("--verbose", action="store_true", help="Show each file being checked")
    args = parser.parse_args(argv)

    failures = check_doc_links(REPO_ROOT, verbose=args.verbose)

    if failures:
        print()
        print(f"[FAIL] Found {len(failures)} broken link(s):")
        print()
        for f in failures:
            print(f"  File:   {f.file}")
            print(f"  Link:   {f.link}")
            print(f"  Reason: {f.reason}")
            print()
        return 1

    print("[OK] All documentation links are valid!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
