#!/usr/bin/env python3
"""Detect leaked agent / tool-call markup in tracked markdown.

Closes a blind spot the other doc gates leave open: ``doc_links.py`` only
resolves the *path* half of a markdown link, ``doc_status_markers.py`` only
inspects status emoji, and ``file_size.py`` only counts bytes. None of them
notice a raw block of Claude tool-invocation XML (``</invoke>``,
``<parameter name=…>``, the ``antml:`` namespace, a bare ``</content>``) left
behind when an agent's tool call leaks into authored prose.

A line is flagged when it contains an unambiguous tool-call token, or when the
*whole* stripped line is a bare content fragment (``<content>`` / ``</content>``)
— the latter scoped to standalone lines so a legitimate ``<content>``
metavariable inside backticks (e.g. ``<timestamp>  <sender>: <content>``) does
not trip. A doc that genuinely needs to show tool-call syntax can opt a line out
with a trailing ``<!-- tool-markup-example -->`` comment.

Usage::

    python scripts/checks/doc_leaked_markup.py [--verbose]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402
from scripts.checks.doc_links import _collect_md_files  # noqa: E402

# Tokens that have no legitimate place in authored markdown — each is a literal
# fragment of Claude tool-call markup. Matched as a substring, anywhere on a line.
_UNAMBIGUOUS_TOKENS = (
    "antml:",
    "<invoke name=",
    "</invoke>",
    "<function_calls>",
    "</function_calls>",
    "<parameter name=",
    "</parameter>",
)

# Content-block fragments that DO collide with a legitimate ``<content>``
# metavariable when embedded in a sentence, so they are flagged only when they
# are the entire (stripped) line — the shape a real leak takes.
_STANDALONE_TOKENS = (
    "<content>",
    "</content>",
)

# A line carrying this marker is documenting tool-call syntax on purpose.
_ALLOW_MARKER = "tool-markup-example"


class LeakIssue(NamedTuple):
    file: str
    line: int
    token: str


def scan_text(rel_path: str, text: str) -> list[LeakIssue]:
    """Return every leaked-markup hit in ``text`` (one per offending line)."""
    issues: list[LeakIssue] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if _ALLOW_MARKER in line:
            continue
        hit = next((t for t in _UNAMBIGUOUS_TOKENS if t in line), None)
        if hit is None and line.strip() in _STANDALONE_TOKENS:
            hit = line.strip()
        if hit is not None:
            issues.append(LeakIssue(file=rel_path, line=idx, token=hit))
    return issues


def check_leaked_markup(repo_root: Path, verbose: bool = False) -> list[LeakIssue]:
    """Scan all tracked markdown for leaked tool-call markup."""
    md_files = _collect_md_files(repo_root)

    issues: list[LeakIssue] = []
    for md_file in md_files:
        rel_path = md_file.relative_to(repo_root).as_posix()
        if verbose:
            print(f"  Checking: {rel_path}")
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        issues.extend(scan_text(rel_path, text))

    _n = len(md_files)
    print(f"[OK] Scanned {_n} markdown file{'s' if _n != 1 else ''} for leaked markup")
    return issues


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(
        description="Detect leaked agent / tool-call markup in markdown.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show each file being checked")
    args = parser.parse_args(argv)

    issues = check_leaked_markup(REPO_ROOT, verbose=args.verbose)

    if issues:
        print()
        print(f"[FAIL] Found {len(issues)} line(s) with leaked tool-call markup:")
        print()
        for i in issues:
            print(f"  File:  {i.file}:{i.line}")
            print(f"  Token: {i.token}")
            print()
        print(
            "These look like leftover agent tool-call tags. Remove them, or mark a "
            "deliberate example with a trailing `<!-- tool-markup-example -->` comment."
        )
        return 1

    print("[OK] No leaked tool-call markup in documentation!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
