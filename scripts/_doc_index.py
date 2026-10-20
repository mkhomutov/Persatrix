"""Shared helpers for INDEX-style doc generators.

The Persatrix repo auto-generates two summary tables from per-file YAML
front-matter:

- ``docs/issues/INDEX.md`` from ``ISSUE-NNNN-*.md`` files (``scripts/issues.py``)
- ``docs/rfcs/INDEX.md``   from ``NNNN-*.md`` RFC files     (``scripts/rfcs.py``)

This module captures the bits the two generators share: a stdlib-only
YAML-subset front-matter parser, ISO-date validation, and a tiny CLI
runner that handles ``--check`` / ``--print`` plumbing. Each generator
keeps its own dataclass, validation rules, and rendering — those are the
parts that genuinely differ.

stdlib-only on purpose: this is build tooling, must run identically on
Windows / macOS / Linux without a PyYAML dependency.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from collections.abc import Callable
from pathlib import Path

FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SCALAR_LINE_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$")
LIST_ITEM_RE = re.compile(r"^\s+-\s+(.*?)\s*$")

REPO_URL = "https://github.com/mkhomutov/Persatrix"


def strip_inline_comment(value: str) -> str:
    """Strip a YAML ``# ...`` trailing comment, respecting quoted strings."""
    in_single = in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return value[:i].rstrip()
    return value.rstrip()


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_front_matter(text: str) -> dict[str, str | list[str]]:
    """Extract a flat ``key -> scalar | list[str]`` mapping.

    Supports the YAML subset used across this repo's front-matter:
    scalar values on one line, and simple ``- item`` lists on
    subsequent indented lines. Nested mappings are not supported.
    """
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}
    out: dict[str, str | list[str]] = {}
    current_list_key: str | None = None
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        item = LIST_ITEM_RE.match(raw_line)
        if item and current_list_key is not None:
            value = unquote(strip_inline_comment(item.group(1)))
            existing = out.get(current_list_key)
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[current_list_key] = [value]
            continue
        m = SCALAR_LINE_RE.match(raw_line)
        if not m:
            current_list_key = None
            continue
        key, value = m.group(1), m.group(2)
        value = unquote(strip_inline_comment(value))
        if value == "":
            # An empty value with subsequent ``- item`` lines means a list.
            current_list_key = key
            out[key] = []
            continue
        current_list_key = None
        out[key] = value
    # Drop empty list shells that never received items, so callers can
    # treat "absent" and "empty" identically.
    return {k: v for k, v in out.items() if v != []}


def is_iso_date(value: str) -> bool:
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def pr_link(number: str) -> str:
    """Render a PR number (no leading ``#``) as a clickable Markdown link."""
    if not number:
        return ""
    return f"[#{number}]({REPO_URL}/pull/{number})"


def run_index_cli(
    *,
    description: str,
    index_file: Path,
    repo_root: Path,
    build_content: Callable[[], tuple[str, int, list[str]]],
    make_target: str,
    compare: Callable[[str, str], str | None] | None = None,
) -> int:
    """Drive the standard ``--check`` / ``--print`` CLI for an INDEX generator.

    ``build_content`` must return ``(content, row_count, errors)``. When
    ``errors`` is non-empty the runner prints them to stderr and exits 1
    before touching ``index_file``.

    ``compare(committed, generated)`` may replace the byte-exact ``--check``
    comparison: it returns ``None`` when the committed file is acceptable and
    a reason string otherwise. The merged-PR history uses it, because that
    file can never contain the merge that lands it.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--check",
        action="store_true",
        help=f"exit 1 if {index_file.name} is stale or any front-matter is invalid",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_table",
        help=f"print the table to stdout in addition to writing {index_file.name}",
    )
    args = parser.parse_args()

    new_content, row_count, errors = build_content()
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    rel = index_file.relative_to(repo_root)
    if args.check:
        current = index_file.read_text(encoding="utf-8") if index_file.exists() else ""
        reason = compare(current, new_content) if compare else (
            None if current == new_content else "is stale"
        )
        if reason is not None:
            print(
                f"error: {rel} {reason} — run `make {make_target}`",
                file=sys.stderr,
            )
            return 1
        return 0

    # Path.open() rather than Path.write_text(newline=...) — the kwarg form is
    # 3.10+, but these regen scripts run under whatever `python3` the pre-commit
    # hook finds on PATH (which can be 3.9). newline="\n" forces LF on write.
    with index_file.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_content)
    print(f"wrote {rel} ({row_count} row(s))")
    if args.print_table:
        print()
        # Re-extract the table block between the auto markers for readable
        # stdout output — keeps `--print` useful without a second renderer.
        m = re.search(r"<!-- BEGIN [^>]+ -->\n(.*?)<!-- END [^>]+ -->", new_content, re.DOTALL)
        if m:
            print(m.group(1), end="")
    return 0
