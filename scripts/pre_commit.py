#!/usr/bin/env python3
"""Unified pre-commit hook — runs fast checks before each commit.

Runs a fast subset of checks (target: <10 s) sequentially and prints a
summary at the end.  Exits 0 if all pass, 1 if any fail.

Checks executed:
  0. Regenerate FILEMAP.md and ``git add`` it
  1. ``go fmt`` check (Go orchestrator)
  2. ``ruff check`` (Python agents)
  3. ``cargo fmt --check`` (Rust CLI)
  4. Doc links check

Usage::

    python scripts/pre_commit.py [--skip-fmt]

Options:
    --skip-fmt   Skip the formatting checks.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_streams  # noqa: E402

_FMT_LABELS = {"go fmt", "cargo fmt"}

_CHECKS: list[tuple[str, list[str]]] = [
    ("go fmt", ["gofmt", "-l", "./internal/", "./cmd/"]),
    ("ruff check", ["{python}", "-m", "ruff", "check", "agents/"]),
    ("cargo fmt", ["cargo", "fmt", "--manifest-path", "cli/Cargo.toml", "--", "--check"]),
    ("doc links", ["{python}", "scripts/checks/doc_links.py"]),
]


def _resolve_argv(argv: list[str]) -> list[str]:
    return [sys.executable if tok == "{python}" else tok for tok in argv]


def _update_filemap() -> bool:
    """Regenerate FILEMAP.md and stage it.  Returns True on success."""
    print("\n▶ filemap")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "scripts/generate_filemap.py"],
            cwd=REPO_ROOT,
        )
        if proc.returncode != 0:
            elapsed = time.monotonic() - t0
            print(f"  ✗ FAIL  ({elapsed:.1f}s)")
            return False
        # Stage the updated file so it's included in the commit.
        subprocess.run(["git", "add", "FILEMAP.md"], cwd=REPO_ROOT, check=True)
        elapsed = time.monotonic() - t0
        print(f"  ✓ PASS  ({elapsed:.1f}s)")
        return True
    except FileNotFoundError:
        elapsed = time.monotonic() - t0
        print(f"  ✗ FAIL  (git or python not found) ({elapsed:.1f}s)")
        return False


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_streams()

    parser = argparse.ArgumentParser(description="Run fast pre-commit checks.")
    parser.add_argument("--skip-fmt", action="store_true", help="Skip formatting checks.")
    args = parser.parse_args(argv)

    # Always regenerate the file map first (and stage it).
    _update_filemap()

    checks = _CHECKS if not args.skip_fmt else [c for c in _CHECKS if c[0] not in _FMT_LABELS]

    results: list[tuple[str, bool, float]] = []
    print("=" * 60)
    print("Pre-commit checks")
    print("=" * 60)

    for label, argv_template in checks:
        cmd = _resolve_argv(argv_template)
        print(f"\n▶ {label}")
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=(label == "go fmt"))
        except FileNotFoundError:
            elapsed = time.monotonic() - t0
            print(f"  ✗ FAIL  (command not found: {cmd[0]})")
            results.append((label, False, elapsed))
            continue
        elapsed = time.monotonic() - t0
        # gofmt -l returns 0 even with unformatted files; check stdout instead.
        if label == "go fmt":
            unformatted = proc.stdout.decode().strip() if proc.stdout else ""
            passed = proc.returncode == 0 and not unformatted
            if unformatted:
                print(unformatted)
        else:
            passed = proc.returncode == 0
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}  ({elapsed:.1f}s)")
        results.append((label, passed, elapsed))

    # Summary
    total_time = sum(r[2] for r in results)
    failed = [r for r in results if not r[1]]

    print("\n" + "=" * 60)
    print(f"Results: {len(results) - len(failed)}/{len(results)} passed  ({total_time:.1f}s)")

    if failed:
        print("\nFailed checks:")
        for label, _, _ in failed:
            print(f"  ✗ {label}")
        print()
        return 1

    print("All checks passed!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
