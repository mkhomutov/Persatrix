#!/usr/bin/env python3
"""Regenerate ``docs/rfcs/INDEX.md`` from per-RFC YAML front-matter.

Each canonical RFC file under ``docs/rfcs/`` named ``NNNN-<slug>.md`` is
expected to start with a YAML front-matter block:

    ---
    id: RFC-0001
    title: Core Orchestration Pipeline
    summary: Planner + State + Registry — the v0.1 orchestration core.
    type: architecture       # feature | architecture | protocol | process
    status: implemented      # see ALLOWED_STATUS below
    author: Maksim Khomutov
    created: 2026-04-08
    target: v0.1 (MVP)
    depends_on:              # documentary only — not surfaced in INDEX
      - RFC-0000
    superseded_by:           # single RFC id, or omit
    ---

This script collects the front-matter from every matching file
(``NNNN-pr-plan.md``, ``NNNN-amendment-*.md``, ``0008-calibration-review.md``,
the ``RFC_TEMPLATE.md``, and ``README.md`` are excluded — those are
companion docs, not standalone RFCs) and writes a Markdown table into
``docs/rfcs/INDEX.md`` between auto-generation markers.

Usage::

    python scripts/rfcs.py            # rewrite INDEX.md
    python scripts/rfcs.py --check    # exit 1 if INDEX.md is stale
    python scripts/rfcs.py --print    # also print the table to stdout

Shares its YAML-subset parser and CLI runner with ``scripts/issues.py``
via ``scripts/_doc_index.py``. Stdlib-only.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _doc_index import (  # noqa: E402  -- path-mutation needed for direct script run
    is_iso_date,
    parse_front_matter,
    run_index_cli,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RFCS_DIR = REPO_ROOT / "docs" / "rfcs"
INDEX_FILE = RFCS_DIR / "INDEX.md"

# Canonical RFCs are ``NNNN-<slug>.md`` where <slug> is the RFC's own
# topic slug. Companion documents — PR plans, amendments, and the
# 0008 calibration review — live in the same directory but are not
# standalone RFCs; they are skipped here and tracked via cross-references
# from the canonical RFC instead.
RFC_FILE_PATTERN = re.compile(r"^\d{4}-[a-z0-9][a-z0-9-]*\.md$")
COMPANION_SUFFIXES = ("-pr-plan", "-calibration-review")
COMPANION_PREFIXES = ("amendment-",)  # used after the ``NNNN-`` numeric prefix
SKIP_NAMES = {"README.md", "INDEX.md", "RFC_TEMPLATE.md"}

# Status vocabulary mirrors the RFC lifecycle in ``docs/rfcs/README.md``.
# Stored lower-case in front-matter; the INDEX renders an emoji marker
# from the table below for human scannability.
ALLOWED_STATUS = {
    "draft",
    "proposed",
    "accepted",
    "implementing",
    "implemented",
    "partially_implemented",
    "rejected",
    "deferred",
    "stable",
    "superseded",
}
ALLOWED_TYPE = {"feature", "architecture", "protocol", "process"}

_STATUS_MARKER = {
    "draft": "🔨 Draft",
    "proposed": "📋 Proposed",
    "accepted": "👍 Accepted",
    "implementing": "🚧 Implementing",
    "implemented": "✅ Implemented",
    "partially_implemented": "⚠️ Partially Implemented",
    "rejected": "❌ Rejected",
    "deferred": "🔮 Deferred",
    "stable": "🚀 Stable",
    "superseded": "🔄 Superseded",
}

# Group "open work" together at the top; "done" / "shelved" at the bottom.
# Within each band, sort by id ascending (RFC number order is meaningful).
_STATUS_ORDER = {
    "implementing": 0,
    "accepted": 1,
    "proposed": 2,
    "draft": 3,
    "partially_implemented": 4,
    "deferred": 5,
    "implemented": 6,
    "stable": 7,
    "superseded": 8,
    "rejected": 9,
}

BEGIN_MARKER = "<!-- BEGIN rfcs:auto -->"
END_MARKER = "<!-- END rfcs:auto -->"


@dataclass
class RFC:
    path: Path
    id: str
    title: str
    summary: str
    type: str
    status: str
    author: str
    created: str
    target: str
    depends_on: list[str] = field(default_factory=list)
    superseded_by: str = ""

    @property
    def link(self) -> str:
        label = self.id or self.path.stem
        return f"[{label}]({self.path.name})"

    @property
    def status_marker(self) -> str:
        return _STATUS_MARKER.get(self.status, self.status)


def _is_companion(name: str) -> bool:
    stem = name.removesuffix(".md")
    # Strip the leading ``NNNN-`` numeric prefix before checking prefixes
    # like ``amendment-``.
    after_number = stem.split("-", 1)[1] if "-" in stem else ""
    return (
        any(stem.endswith(suffix) for suffix in COMPANION_SUFFIXES)
        or any(after_number.startswith(prefix) for prefix in COMPANION_PREFIXES)
    )


def _scalar(fm: dict[str, str | list[str]], key: str) -> str:
    value = fm.get(key, "")
    return value if isinstance(value, str) else ""


def _list(fm: dict[str, str | list[str]], key: str) -> list[str]:
    value = fm.get(key, [])
    if isinstance(value, list):
        return value
    return [value] if value else []


def collect_rfcs() -> tuple[list[RFC], list[str]]:
    """Collect every canonical RFC and report any that lack front-matter.

    A canonical RFC file (matching ``RFC_FILE_PATTERN`` and not a companion)
    must have a YAML front-matter block; otherwise it would silently drop
    out of INDEX.md while still being a tracked RFC, which is a real drift
    vector. Reported as a hard error rather than a warning so CI catches it.
    """
    rfcs: list[RFC] = []
    errors: list[str] = []
    for path in sorted(RFCS_DIR.glob("*.md")):
        if path.name in SKIP_NAMES:
            continue
        if not RFC_FILE_PATTERN.match(path.name):
            continue
        if _is_companion(path.name):
            continue
        text = path.read_text(encoding="utf-8")
        fm = parse_front_matter(text)
        if not fm:
            errors.append(
                f"{path.name}: missing YAML front-matter (required for INDEX generation)"
            )
            continue
        rfcs.append(
            RFC(
                path=path,
                id=_scalar(fm, "id"),
                title=_scalar(fm, "title"),
                summary=_scalar(fm, "summary"),
                type=_scalar(fm, "type").strip().lower(),
                status=_scalar(fm, "status").strip().lower(),
                author=_scalar(fm, "author"),
                created=_scalar(fm, "created"),
                target=_scalar(fm, "target"),
                depends_on=_list(fm, "depends_on"),
                superseded_by=_scalar(fm, "superseded_by"),
            )
        )
    return rfcs, errors


_ID_RE = re.compile(r"^RFC-\d{4}$")


def validate(rfcs: list[RFC]) -> list[str]:
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    known_ids = {r.id for r in rfcs if r.id}
    for rfc in rfcs:
        loc = rfc.path.name
        if not rfc.id:
            errors.append(f"{loc}: missing 'id'")
        elif not _ID_RE.match(rfc.id):
            errors.append(f"{loc}: invalid id '{rfc.id}' (expected RFC-NNNN)")
        elif rfc.id in seen_ids:
            errors.append(f"{loc}: duplicate id {rfc.id} (also in {seen_ids[rfc.id]})")
        else:
            seen_ids[rfc.id] = loc
        if not rfc.title:
            errors.append(f"{loc}: missing 'title'")
        if not rfc.summary:
            errors.append(f"{loc}: missing 'summary'")
        if rfc.type and rfc.type not in ALLOWED_TYPE:
            errors.append(f"{loc}: invalid type '{rfc.type}' (allowed: {sorted(ALLOWED_TYPE)})")
        if rfc.status and rfc.status not in ALLOWED_STATUS:
            errors.append(f"{loc}: invalid status '{rfc.status}' (allowed: {sorted(ALLOWED_STATUS)})")
        if rfc.created and not is_iso_date(rfc.created):
            errors.append(f"{loc}: invalid 'created' date '{rfc.created}' (expected YYYY-MM-DD)")
        if rfc.superseded_by and rfc.superseded_by not in known_ids:
            errors.append(
                f"{loc}: 'superseded_by: {rfc.superseded_by}' references an RFC id not present in docs/rfcs/"
            )
        for dep in rfc.depends_on:
            if not _ID_RE.match(dep):
                errors.append(
                    f"{loc}: invalid 'depends_on' entry '{dep}' (expected RFC-NNNN)"
                )
            elif dep not in known_ids:
                errors.append(
                    f"{loc}: 'depends_on: {dep}' references an RFC id not present in docs/rfcs/"
                )
    return errors


def _sort_key(r: RFC) -> tuple[int, str]:
    return (_STATUS_ORDER.get(r.status, 99), r.id)


_HEADER = "| ID | Status | Type | Target | Created | Title |"
_DIVIDER = "|----|--------|------|--------|---------|-------|"


def render_table(rfcs: list[RFC]) -> str:
    if not rfcs:
        return f"{_HEADER}\n{_DIVIDER}\n| -- | *(no RFCs)* | | | | |\n"
    rows = [_HEADER, _DIVIDER]
    for r in sorted(rfcs, key=_sort_key):
        rows.append(
            f"| {r.link} | {r.status_marker} | {r.type} | {r.target} "
            f"| {r.created} | {r.title} |"
        )
    return "\n".join(rows) + "\n"


_OPEN_STATUSES = {"draft", "proposed", "accepted", "implementing", "partially_implemented"}
_DONE_STATUSES = {"implemented", "stable"}


def render_index(rfcs: list[RFC]) -> str:
    open_count = sum(1 for r in rfcs if r.status in _OPEN_STATUSES)
    done_count = sum(1 for r in rfcs if r.status in _DONE_STATUSES)
    deferred_count = sum(1 for r in rfcs if r.status == "deferred")
    table = render_table(rfcs)
    return (
        "# Persatrix RFCs — Index\n"
        "\n"
        f"> {open_count} in-flight · {done_count} implemented/stable · "
        f"{deferred_count} deferred · "
        "auto-generated by `make rfcs` (do not hand-edit between markers).\n"
        "\n"
        "See [README.md](README.md) for RFC process, format, and lifecycle. "
        "Reserved RFC numbers and roadmap rollups live in "
        "[ROADMAP.md](../../ROADMAP.md#rfc-master-index).\n"
        "\n"
        f"{BEGIN_MARKER}\n"
        f"{table}"
        f"{END_MARKER}\n"
    )


def _build() -> tuple[str, int, list[str]]:
    rfcs, collect_errors = collect_rfcs()
    errors = collect_errors + validate(rfcs)
    return render_index(rfcs), len(rfcs), errors


def main() -> int:
    return run_index_cli(
        description=__doc__.split("\n", 1)[0],
        index_file=INDEX_FILE,
        repo_root=REPO_ROOT,
        build_content=_build,
        make_target="rfcs",
    )


if __name__ == "__main__":
    sys.exit(main())
