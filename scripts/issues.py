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

Shares its YAML-subset parser and CLI runner with ``scripts/rfcs.py``
via ``scripts/_doc_index.py``. Uses only the Python stdlib so it runs
identically on Windows, macOS, and Linux without WSL or GNU coreutils.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _doc_index import (  # noqa: E402  -- path-mutation needed for direct script run
    is_iso_date,
    parse_front_matter,
    pr_link,
    run_index_cli,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUES_DIR = REPO_ROOT / "docs" / "issues"
INDEX_FILE = ISSUES_DIR / "INDEX.md"
TEMPLATE_NAME = "ISSUE-TEMPLATE.md"

ISSUE_FILE_PATTERN = re.compile(r"^ISSUE-\d{4}-[a-z0-9-]+\.md$")

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


def _slug_from_filename(name: str) -> str:
    return name.removeprefix("ISSUE-").removesuffix(".md").split("-", 1)[1]


def _scalar(fm: dict[str, str | list[str]], key: str) -> str:
    value = fm.get(key, "")
    return value if isinstance(value, str) else ""


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
        fm = parse_front_matter(path.read_text(encoding="utf-8"))
        issues.append(
            Issue(
                path=path,
                id=_scalar(fm, "id"),
                slug=_slug_from_filename(path.name),
                status=_scalar(fm, "status"),
                severity=_scalar(fm, "severity"),
                area=_scalar(fm, "area").strip().lower(),
                created=_scalar(fm, "created"),
                closed=_scalar(fm, "closed"),
                closed_pr=_scalar(fm, "closed_pr"),
                summary=_scalar(fm, "summary"),
            )
        )
    return issues


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
        if issue.created and not is_iso_date(issue.created):
            errors.append(f"{loc}: invalid 'created' date '{issue.created}' (expected YYYY-MM-DD)")
        if issue.closed and not is_iso_date(issue.closed):
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
            f"| {i.closed} | {pr_link(i.closed_pr)} | {i.summary} |"
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


def _build() -> tuple[str, int, list[str]]:
    issues = collect_issues()
    errors = validate(issues)
    return render_index(issues), len(issues), errors


def main() -> int:
    return run_index_cli(
        description=__doc__.split("\n", 1)[0],
        index_file=INDEX_FILE,
        repo_root=REPO_ROOT,
        build_content=_build,
        make_target="issues",
    )


if __name__ == "__main__":
    sys.exit(main())
