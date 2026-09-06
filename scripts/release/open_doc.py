#!/usr/bin/env python3
"""Open a version-cycle document from its template with the placeholders filled.

Each release produces the same documents in the same order (the release
cycle, ``docs/methodology/release-cycle.md``); each starts as a copy of a
template under ``docs/templates/`` with ``vX.Y.Z``, ``<Codename>``, the
previous version and the date typed in, and the ``> Guidance:`` blockquotes
deleted. This script does that copy. It never overwrites.

Usage::

    python scripts/release/open_doc.py --kind release-checklist --version 0.3.16 \\
        --codename "Who is listening"
    python scripts/release/open_doc.py --kind plan --version 0.4.0 --codename "Agent Organizations"

Kinds: plan, scope-locks, release-prep-plan, release-baseline,
release-checklist, execution-report. The previous version is read from
``CHANGELOG.md``'s newest dated heading unless ``--previous`` is given.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_streams  # noqa: E402
from scripts.checks.released import released_versions  # noqa: E402

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: kind → (template path, output path pattern)
KINDS: dict[str, tuple[str, str]] = {
    "plan": ("docs/templates/VERSION_PLAN_TEMPLATE.md", "docs/v{version}-plan.md"),
    "scope-locks": ("docs/templates/SCOPE_LOCKS_TEMPLATE.md", "docs/v{version}-scope-locks.md"),
    "release-prep-plan": (
        "docs/templates/RELEASE_PREP_PLAN_TEMPLATE.md",
        "docs/v{version}-release-prep-plan.md",
    ),
    "release-baseline": (
        "docs/templates/RELEASE_BASELINE_TEMPLATE.md",
        "docs/v{version}-release-baseline.md",
    ),
    "release-checklist": (
        "docs/templates/RELEASE_CHECKLIST_TEMPLATE.md",
        "docs/v{version}-release-checklist.md",
    ),
    "execution-report": (
        "docs/templates/EXECUTION_REPORT_TEMPLATE.md",
        "docs/manual-tests/v{version}-execution-report.md",
    ),
}


def _strip_guidance(text: str) -> str:
    """Drop every ``> Guidance:`` blockquote (through its next blank line)."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith("> Guidance:"):
            skipping = True
            continue
        if skipping:
            if line.startswith(">"):
                continue
            skipping = False
            if line.strip() == "" and out and out[-1].strip() == "":
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def _parts(version: str) -> tuple[int, int, int]:
    m = _VERSION_RE.match(version)
    if not m:
        raise ValueError(f"version must be X.Y.Z, got {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def fill(text: str, *, version: str, codename: str, previous: str, today: str) -> str:
    """Replace the template placeholders. Order matters: longer forms first.

    Mechanical placeholders only. ``<n>``, ``<sha>``, ``<MT ID>`` and the
    dates of amendments that do not exist yet stay for the author.
    """
    major, minor, patch = _parts(version)
    out = text
    out = out.replace("vX.Y.(Z-1)", f"v{previous}").replace("X.Y.(Z-1)", previous)
    out = out.replace("vX.Y.(Z+1)", f"v{major}.{minor}.{patch + 1}")
    out = out.replace("X.Y.(Z+1)", f"{major}.{minor}.{patch + 1}")
    out = out.replace("vX.Y.Z", f"v{version}").replace("X.Y.Z", version)
    # Branch-prefix digits: v0.3.16 → v0316 (the cycle convention, e.g. feature/v0316-).
    out = out.replace("vXYZ", f"v{major}{minor}{patch}")
    out = out.replace(
        "`docs/v<line>.x-sequencing.md` (the current line's sequencing doc)",
        f"`docs/v{major}.{minor}.x-sequencing.md`",
    )
    out = out.replace("<Codename>", codename)
    out = out.replace("**Created**: YYYY-MM-DD", f"**Created**: {today}")
    out = out.replace("(baseline, YYYY-MM-DD)", f"(baseline, {today})")
    out = out.replace("plan opening (YYYY-MM-DD)", f"plan opening ({today})")
    return _strip_guidance(out)


def _previous_version(repo_root: Path, version: str) -> str:
    """Newest dated CHANGELOG version below *version*; raises when there is none."""
    target = _parts(version)
    older = sorted((v for v in released_versions(repo_root) if _parts(v) < target), key=_parts)
    if not older:
        raise ValueError(
            "no released version older than the target in CHANGELOG.md — pass --previous"
        )
    return older[-1]


def open_doc(
    repo_root: Path, *, kind: str, version: str, codename: str, today: str,
    previous: str | None = None, force: bool = False,
) -> Path:
    template_rel, out_pattern = KINDS[kind]
    template = (repo_root / template_rel).read_text(encoding="utf-8")
    out_path = repo_root / out_pattern.format(version=version)
    if out_path.exists() and not force:
        raise FileExistsError(f"{out_path} already exists (pass --force to overwrite)")
    prev = previous or _previous_version(repo_root, version)
    content = fill(template, version=version, codename=codename, previous=prev, today=today)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return out_path


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_streams()
    parser = argparse.ArgumentParser(description="Open a version-cycle document from its template.")
    parser.add_argument("--kind", required=True, choices=sorted(KINDS))
    parser.add_argument("--version", required=True, help="X.Y.Z (no leading v)")
    parser.add_argument("--codename", required=True)
    parser.add_argument(
        "--previous", help="previous version (default: newest dated CHANGELOG heading)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)
    try:
        path = open_doc(
            REPO_ROOT, kind=args.kind, version=args.version.lstrip("v"), codename=args.codename,
            today=_dt.date.today().isoformat(), previous=args.previous, force=args.force,
        )
    except (FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path.relative_to(REPO_ROOT)} — fill the <placeholders>, then `git add` it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
