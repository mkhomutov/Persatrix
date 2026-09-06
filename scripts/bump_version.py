#!/usr/bin/env python3
"""Bump version strings across all Persatrix components.

Usage:
    python scripts/bump_version.py 0.3.0
    python scripts/bump_version.py 0.3.0 --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that contain the project version and need updating.
# Each entry: (relative path, regex pattern, replacement template).
# A file may appear more than once when it carries the version in more
# than one spot; each entry is applied independently (one substitution
# per entry), so the patterns must not overlap.
VERSION_FILES: list[tuple[str, str, str]] = [
    (
        "cli/Cargo.toml",
        r'^(version\s*=\s*")[^"]+(")$',
        r"\g<1>{version}\2",
    ),
    (
        "agents/pyproject.toml",
        r'^(version\s*=\s*")[^"]+(")$',
        r"\g<1>{version}\2",
    ),
    # The observability runtimes hardcode a service.version default used
    # when PERSATRIX_SERVICE_VERSION is unset. These are not build inputs,
    # so a stale value never fails `make all` — it must be bumped here or
    # it silently drifts. tracing.py also restates the default in its
    # module docstring, hence the second tracing.py entry.
    (
        "agents/observability/metrics.py",
        r'^(_DEFAULT_SERVICE_VERSION\s*=\s*")[^"]+(")$',
        r"\g<1>{version}\2",
    ),
    (
        "agents/observability/tracing.py",
        r'^(_DEFAULT_SERVICE_VERSION\s*=\s*")[^"]+(")$',
        r"\g<1>{version}\2",
    ),
    (
        "agents/observability/tracing.py",
        r'(\(default: ")\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?(")',
        r"\g<1>{version}\2",
    ),
    # The Go orchestrator's compiled-in service-version fallback, surfaced by
    # the web console's /api/v1/ui/config when PERSATRIX_SERVICE_VERSION is
    # unset and the build carries no module version. Like the Python defaults
    # above it is not a build input, so it must be bumped here or it drifts.
    (
        "internal/server/ui_handlers.go",
        r'^(const defaultServiceVersion = ")[^"]+(")$',
        r"\g<1>{version}\2",
    ),
]

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")


def bump(version: str, *, dry_run: bool = False) -> list[str]:
    """Update version in all tracked files. Returns list of changed paths."""
    if not SEMVER_RE.match(version):
        print(
            f"error: '{version}' is not valid semver (expected X.Y.Z[-prerelease])",
            file=sys.stderr,
        )
        sys.exit(1)

    changed: list[str] = []

    for rel_path, pattern, template in VERSION_FILES:
        path = ROOT / rel_path
        if not path.exists():
            print(f"  SKIP  {rel_path} (file not found)")
            continue

        text = path.read_text(encoding="utf-8")
        replacement = template.format(version=version)
        new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)

        if count == 0:
            print(f"  WARN  {rel_path} — pattern not matched, skipping")
            continue

        if new_text == text:
            print(f"  OK    {rel_path} (already {version})")
            continue

        if dry_run:
            print(f"  WOULD {rel_path}")
        else:
            path.write_text(new_text, encoding="utf-8")
            print(f"  DONE  {rel_path}")
        changed.append(rel_path)

    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump Persatrix version across all components")
    parser.add_argument("version", help="New version string (semver, e.g. 0.3.0)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing",
    )
    args = parser.parse_args()

    print(f"Bumping to {args.version}{' (dry run)' if args.dry_run else ''}:")
    changed = bump(args.version, dry_run=args.dry_run)

    if not changed:
        print("\nNo files changed.")
    else:
        # A file may take more than one substitution (see VERSION_FILES);
        # count distinct paths so the summary matches the file count.
        distinct = len(set(changed))
        print(f"\n{distinct} file(s) {'would be ' if args.dry_run else ''}updated.")

    # Remind about manual steps
    print("\nRemaining manual steps:")
    print("  1. cd cli && cargo update --workspace   # regenerate Cargo.lock")
    print(f"  2. git-cliff --tag v{args.version} ...            # update CHANGELOG.md")
    print(f"  3. git tag -a v{args.version} -m 'v{args.version}'         # tag release")
    print("  4. See docs/guides/version-bump.md for the full checklist")


if __name__ == "__main__":
    main()
