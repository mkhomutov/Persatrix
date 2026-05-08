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
import subprocess
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

# Patterns to strip before scanning for links (code blocks and inline code
# can contain bracket/paren sequences that look like markdown links).
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"``[^`]+``|`[^`]+`")


def _strip_inline_code_outside_links(text: str) -> str:
    """Remove backtick spans that are not part of markdown link text.

    Inline code like ``[a-z0-9]`` looks like a markdown link after backtick
    stripping.  This function removes backtick content only when it appears
    *outside* of ``[text](url)`` link syntax, so real link text is preserved.
    """
    link_positions: set[int] = set()
    for lm in _LINK_RE.finditer(text):
        for pos in range(lm.start(), lm.end()):
            link_positions.add(pos)
    result: list[str] = []
    last = 0
    for cm in _INLINE_CODE_RE.finditer(text):
        if cm.start() in link_positions:
            continue  # inside a link — keep it
        result.append(text[last:cm.start()])
        last = cm.end()
    result.append(text[last:])
    return "".join(result)


class BrokenLink(NamedTuple):
    file: str
    link: str
    target: str
    reason: str


def _collect_md_files(repo_root: Path) -> list[Path]:
    """Return every tracked markdown file under ``repo_root``.

    ISSUE-0036: the source of truth is ``git ls-files '*.md'``, so the
    set automatically excludes (a) untracked working-tree artifacts
    (e.g. ``PR_BODY.md`` left behind by ``git stash``), (b) files under
    ``.git/``, and (c) gitignored paths — without ad-hoc filter chains.
    Crucially, it also catches markdown files at depth ≥ 3 outside
    ``docs/`` (e.g. ``.github/instructions/*.md``,
    ``prompts/runtime/safety/*.md``) which the previous glob shape
    silently dropped from the link scan.

    ``docs/pr-reviews/`` stays excluded by project convention (see
    ``.github/copilot-instructions.md``) — PR review reports are
    local-only artifacts, gitignored anyway, and their links resolve
    against repo-root paths rather than ``docs/pr-reviews/``.

    Falls back to a glob-and-filter walk when ``repo_root`` is not a
    git checkout (downstream tarball consumers, extracted-source
    builds). The fallback prints a one-line WARN so the divergence is
    visible.
    """
    pr_reviews_dir = (repo_root / "docs" / "pr-reviews").resolve()
    pr_reviews_prefix = os.path.normcase(str(pr_reviews_dir)) + os.sep

    tracked = _git_ls_md_files(repo_root)
    if tracked is None:
        files = _glob_md_files_fallback(repo_root)
    else:
        files = sorted(tracked)

    return [
        f for f in files
        if not os.path.normcase(str(f.resolve())).startswith(pr_reviews_prefix)
    ]


def _git_ls_md_files(repo_root: Path) -> list[Path] | None:
    """Return ``git ls-files '*.md'`` resolved against ``repo_root``.

    Returns ``None`` when the call is not viable (no ``git`` on PATH,
    or ``repo_root`` is outside a working tree). The caller falls back
    to the glob walk in those cases.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "*.md"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    return [
        repo_root / Path(name.decode("utf-8"))
        for name in result.stdout.split(b"\0")
        if name
    ]


def _glob_md_files_fallback(repo_root: Path) -> list[Path]:
    """Legacy glob walk used when git is unavailable.

    Preserved verbatim from the pre-ISSUE-0036 implementation so
    downstream tarball consumers still get a useful link scan. Walks
    ``docs/**/*.md`` plus repo-root-and-one-level deep, with the same
    ``.git/`` and ``docs/`` filters as before.
    """
    print(
        "[WARN] doc_links: git ls-files unavailable; falling back to glob "
        "walk (depth-limited).",
        file=sys.stderr,
    )

    docs_dir = repo_root / "docs"
    files: list[Path] = []

    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.rglob("*.md")))

    root_files = set(repo_root.glob("*.md"))
    root_files.update(repo_root.glob("*/*.md"))
    git_dir = os.path.normcase(str((repo_root / ".git").resolve())) + os.sep
    root_files = {
        f for f in root_files
        if not os.path.normcase(str(f.resolve())).startswith(git_dir)
    }
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

        # Strip code blocks and inline code to avoid false positives
        # from regex patterns like [a-z0-9] being parsed as links.
        # Only strip backtick content OUTSIDE of markdown link text brackets.
        stripped = _CODE_BLOCK_RE.sub("", content)
        stripped = _strip_inline_code_outside_links(stripped)

        file_dir = md_file.parent

        for match in _LINK_RE.finditer(stripped):
            link_path = match.group(2)
            anchor = match.group(3) or ""
            full_link = link_path + anchor

            if re.match(r"^https?://", link_path) or link_path.startswith("mailto:"):
                continue
            if re.match(r"^[a-z]+://", link_path):
                continue
            # Skip regex-like targets that leaked through backtick stripping.
            if re.search(r"[*+?^$|\\]", link_path):
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
