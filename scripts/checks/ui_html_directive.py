#!/usr/bin/env python3
"""Reject Svelte's ``{@html …}`` directive anywhere under ``web/src``.

RFC 0039 enabled-mode exposure amendment §A3: the web console renders
persona- and LLM-authored channel content — untrusted text by
construction — and the browser session is a cookie, so an XSS is
session riding. The console already avoids ``{@html}`` by convention
(see the comments in ``web/src/panels/ChannelMessage.svelte`` and
``web/src/lib/mentions.js``); this check promotes that convention to a
CI gate so the discipline survives a contributor who has not read
those comments.

Matched form: ``{@html`` followed by whitespace — the directive always
carries an expression (``{@html expr}``). A bare ``{@html}`` in a
comment (prose *about* the directive, which is exactly what the two
existing files contain) does not trip.

Usage::

    python scripts/checks/ui_html_directive.py [--verbose]
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

WEB_SRC = REPO_ROOT / "web" / "src"

# The directive form: `{@html` + whitespace + an expression. Svelte
# requires the whitespace, so this cannot false-negative a real use.
_DIRECTIVE = re.compile(r"\{@html\s")

# Source extensions the Svelte compiler (or a template string feeding
# it) could carry the directive in.
_EXTENSIONS = {".svelte", ".js", ".ts", ".html"}


class Finding(NamedTuple):
    path: Path
    line_no: int
    line: str


def find_html_directives(root: Path = WEB_SRC) -> list[Finding]:
    """Return every ``{@html …}`` directive use under *root*."""
    findings: list[Finding] = []
    if not root.is_dir():
        return findings
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in _EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _DIRECTIVE.search(line):
                findings.append(Finding(path, line_no, line.strip()))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="list scanned root")
    args = parser.parse_args()
    ensure_utf8_stdout()

    if args.verbose:
        print(f"Scanning {WEB_SRC.relative_to(REPO_ROOT)} for {{@html …}} directives")

    findings = find_html_directives()
    if findings:
        print("ERROR: {@html} directive found under web/src — the console renders")
        print("untrusted (LLM-authored) content; render as text instead (RFC 0039")
        print("enabled-mode exposure amendment §A3).")
        for f in findings:
            rel = f.path.relative_to(REPO_ROOT)
            print(f"  {rel}:{f.line_no}: {f.line}")
        return 1

    print("ui_html_directive: OK (no {@html} directive under web/src)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
