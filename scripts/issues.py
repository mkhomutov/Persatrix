#!/usr/bin/env python3
"""Regenerate ``docs/issues/INDEX.md`` from per-issue front-matter.

Each issue file under ``docs/issues/`` named ``ISSUE-NNNN-<slug>.md`` is
expected to start with a YAML front-matter block:

    ---
    id: ISSUE-0007
    summary: cost rounding drifts on long sessions   # one-line, surfaced in INDEX
    status: open            # open | in_progress | resolved
    severity: medium        # low | medium | high | critical
    area: cost
    created: 2026-05-02
    closed: 2026-05-14      # required when status == resolved
    closed_pr: 312          # rendered as a clickable #312 link in INDEX
    refs:                   # documentary only — not surfaced in INDEX
      - docs/rfcs/0009-pr-plan.md
    ---

This script collects the front-matter from every matching file (the
template ``ISSUE-TEMPLATE.md`` is excluded) and writes a Markdown table
into ``docs/issues/INDEX.md`` between auto-generation markers.

Usage::

    python scripts/issues.py            # rewrite INDEX.md
    python scripts/issues.py --check    # exit 1 if INDEX.md is stale
    python scripts/issues.py --print    # also print the table to stdout

Uses only the Python stdlib so it runs identically on Windows, macOS,
and Linux without WSL or GNU coreutils.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUES_DIR = REPO_ROOT / "docs" / "issues"
INDEX_FILE = ISSUES_DIR / "INDEX.md"
TEMPLATE_NAME = "ISSUE-TEMPLATE.md"
REPO_URL = "https://github.com/mkhomutov/Persatrix"

ISSUE_FILE_PATTERN = re.compile(r"^ISSUE-\d{4}-[a-z0-9-]+\.md$")
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SCALAR_LINE_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$")

ALLOWED_STATUS = {"open", "in_progress", "resolved"}
ALLOWED_SEVERITY = {"low", "medium", "high", "critical"}

BEGIN_MARKER = "<!-- BEGIN issues:auto -->"
END_MARKER = "<!-- END issues:auto -->"


@dataclass
class Issue:
    path: Path
    id: str
    slug: str
    status: str
    severity: str
    area: str
    created: str
    closed: str
    closed_pr: str
    summary: str

    @property
    def link(self) -> str:
        return f"[{self.id}]({self.path.name})"

    @property
    def closed_pr_link(self) -> str:
        if not self.closed_pr:
            return ""
        return f"[#{self.closed_pr}]({REPO_URL}/pull/{self.closed_pr})"


def _strip_inline_comment(value: str) -> str:
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


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_front_matter(text: str) -> dict[str, str]:
    """Extract a flat ``key -> scalar`` mapping. List/block values are skipped."""
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        if not raw_line or raw_line.lstrip().startswith("#"):
            continue
        m = SCALAR_LINE_RE.match(raw_line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        value = _unquote(_strip_inline_comment(value))
        if value == "":
            continue
        out[key] = value
    return out


def _slug_from_filename(name: str) -> str:
    return name.removeprefix("ISSUE-").removesuffix(".md").split("-", 1)[1]


def collect_issues() -> list[Issue]:
    issues: list[Issue] = []
    for path in sorted(ISSUES_DIR.glob("ISSUE-*.md")):
        if path.name == TEMPLATE_NAME:
            continue
        if not ISSUE_FILE_PATTERN.match(path.name):
            print(
                f"warning: {path.name} does not match ISSUE-NNNN-slug.md naming",
                file=sys.stderr,
            )
            continue
        fm = _parse_front_matter(path.read_text(encoding="utf-8"))
        issues.append(
            Issue(
                path=path,
                id=fm.get("id", ""),
                slug=_slug_from_filename(path.name),
                status=fm.get("status", ""),
                severity=fm.get("severity", ""),
                area=fm.get("area", "").strip().lower(),
                created=fm.get("created", ""),
                closed=fm.get("closed", ""),
                closed_pr=fm.get("closed_pr", ""),
                summary=fm.get("summary", ""),
            )
        )
    return issues


def _is_iso_date(value: str) -> bool:
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate(issues: list[Issue]) -> list[str]:
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    for issue in issues:
        loc = issue.path.name
        if not issue.id:
            errors.append(f"{loc}: missing 'id'")
        elif issue.id in seen_ids:
            errors.append(f"{loc}: duplicate id {issue.id} (also in {seen_ids[issue.id]})")
        else:
            seen_ids[issue.id] = loc
        if issue.status and issue.status not in ALLOWED_STATUS:
            errors.append(f"{loc}: invalid status '{issue.status}' (allowed: {sorted(ALLOWED_STATUS)})")
        if issue.severity and issue.severity not in ALLOWED_SEVERITY:
            errors.append(f"{loc}: invalid severity '{issue.severity}' (allowed: {sorted(ALLOWED_SEVERITY)})")
        if issue.status == "resolved" and not issue.closed:
            errors.append(f"{loc}: status=resolved requires 'closed' date")
        if issue.created and not _is_iso_date(issue.created):
            errors.append(f"{loc}: invalid 'created' date '{issue.created}' (expected YYYY-MM-DD)")
        if issue.closed and not _is_iso_date(issue.closed):
            errors.append(f"{loc}: invalid 'closed' date '{issue.closed}' (expected YYYY-MM-DD)")
    return errors


_STATUS_ORDER = {"in_progress": 0, "open": 1, "resolved": 2}
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _sort_key(i: Issue) -> tuple[int, int, str]:
    return (
        _STATUS_ORDER.get(i.status, 99),
        _SEVERITY_ORDER.get(i.severity, 99),
        i.id,
    )


_HEADER = "| ID | Status | Severity | Area | Created | Closed | Closed PR | Summary |"
_DIVIDER = "|----|--------|----------|------|---------|--------|-----------|---------|"


def render_table(issues: list[Issue]) -> str:
    if not issues:
        return f"{_HEADER}\n{_DIVIDER}\n| -- | *(no issues)* | | | | | | |\n"
    rows = [_HEADER, _DIVIDER]
    for i in sorted(issues, key=_sort_key):
        rows.append(
            f"| {i.link} | {i.status} | {i.severity} | {i.area} | {i.created} "
            f"| {i.closed} | {i.closed_pr_link} | {i.summary} |"
        )
    return "\n".join(rows) + "\n"


def render_index(issues: list[Issue]) -> str:
    open_count = sum(1 for i in issues if i.status in ("open", "in_progress"))
    resolved_count = sum(1 for i in issues if i.status == "resolved")
    table = render_table(issues)
    return (
        "# Persatrix Issues — Index\n"
        "\n"
        f"> {open_count} open / in_progress · {resolved_count} resolved · "
        "auto-generated by `make issues` (do not hand-edit between markers).\n"
        "\n"
        "See [README.md](README.md) for conventions and lifecycle.\n"
        "\n"
        f"{BEGIN_MARKER}\n"
        f"{table}"
        f"{END_MARKER}\n"
    )


def write_index(content: str) -> None:
    INDEX_FILE.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if INDEX.md is stale or any front-matter is invalid",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_table",
        help="print the table to stdout in addition to writing INDEX.md",
    )
    args = parser.parse_args()

    issues = collect_issues()
    errors = validate(issues)
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    new_content = render_index(issues)

    if args.check:
        current = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else ""
        if current != new_content:
            print(
                f"error: {INDEX_FILE.relative_to(REPO_ROOT)} is stale — run `make issues`",
                file=sys.stderr,
            )
            return 1
        return 0

    write_index(new_content)
    print(f"wrote {INDEX_FILE.relative_to(REPO_ROOT)} ({len(issues)} issue(s))")
    if args.print_table:
        print()
        print(render_table(issues), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
