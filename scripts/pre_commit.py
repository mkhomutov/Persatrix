#!/usr/bin/env python3
"""Unified pre-commit hook — runs fast checks before each commit.

Runs a fast subset of checks (target: <10 s) sequentially and prints a
summary at the end.  Exits 0 if all pass, 1 if any fail.

Checks executed:
  0. Regenerate FILEMAP.md and docs/merged-prs.md and ``git add`` them
  1. ``go fmt`` check (Go orchestrator)
  2. ``ruff check`` (Python agents)
  3. ``cargo fmt --check`` (Rust CLI)
  4. Doc links check
  5. Leaked tool-call markup check (docs)
  6. Doc status markers check
  7. RFC index freshness (docs/rfcs/INDEX.md up to date with front-matter)
  8. File size check (code: ≤500 lines, docs: ≤3000 words)
  9. Plan status (no "PR open" row for a PR that has merged)

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


def _staged_go_files() -> list[str]:
    """Return paths of .go files staged for commit, under internal/ or cmd/."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    paths = proc.stdout.decode(errors="replace").splitlines()
    return [
        p for p in paths
        if p.endswith(".go") and (p.startswith("internal/") or p.startswith("cmd/"))
    ]


def _gofmt_staged_blobs(paths: list[str]) -> list[str]:
    """Check gofmt against the staged blob content for each path.

    Reads each staged blob via ``git show :path`` and pipes it into ``gofmt -l``
    on stdin.  This sidesteps the Windows ``core.autocrlf=true`` case where
    ``git ls-files --eol`` reports ``i/lf w/crlf`` — the index has LF, the
    working tree has CRLF, and reading the working-tree file would make gofmt
    flag it as unformatted even though the committed content is fine.

    Returns the subset of paths whose staged content is not gofmt-clean.
    """
    unformatted: list[str] = []
    for path in paths:
        try:
            show = subprocess.run(
                ["git", "show", f":{path}"],
                cwd=REPO_ROOT,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            unformatted.append(path)
            continue
        if show.returncode != 0:
            unformatted.append(path)
            continue
        try:
            fmt = subprocess.run(
                ["gofmt", "-l"],
                cwd=REPO_ROOT,
                input=show.stdout,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            raise
        if fmt.returncode != 0 or fmt.stdout.strip():
            unformatted.append(path)
    return unformatted


_CHECKS: list[tuple[str, list[str]]] = [
    ("go fmt", ["gofmt", "-l", "./internal/", "./cmd/"]),
    ("ruff check", ["{python}", "-m", "ruff", "check", "agents/"]),
    ("cargo fmt", ["cargo", "fmt", "--manifest-path", "cli/Cargo.toml", "--", "--check"]),
    ("doc links", ["{python}", "scripts/checks/doc_links.py"]),
    ("doc markup", ["{python}", "scripts/checks/doc_leaked_markup.py"]),
    ("doc status", ["{python}", "scripts/checks/doc_status_markers.py"]),
    ("rfcs index", ["{python}", "scripts/rfcs.py", "--check"]),
    ("file size", ["{python}", "scripts/checks/file_size.py", "--strict"]),
    ("plan status", ["{python}", "scripts/checks/plan_status.py"]),
]


def _resolve_argv(argv: list[str]) -> list[str]:
    return [sys.executable if tok == "{python}" else tok for tok in argv]


_GENERATED = (
    # (label, generator script, output path to stage) — regenerated before the
    # checks so the commit carries the current file map and merged-PR history.
    ("filemap", "scripts/generate_filemap.py", "FILEMAP.md"),
    ("merged prs", "scripts/merged_prs.py", "docs/merged-prs.md"),
)


def _regenerate(label: str, script: str, output: str) -> bool:
    """Run one generator and stage its output.  Returns True on success."""
    print(f"\n▶ {label}")
    t0 = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, script], cwd=REPO_ROOT)
        if proc.returncode != 0:
            elapsed = time.monotonic() - t0
            print(f"  ✗ FAIL  ({elapsed:.1f}s)")
            return False
        # Stage the updated file so it's included in the commit.
        subprocess.run(["git", "add", output], cwd=REPO_ROOT, check=True)
        elapsed = time.monotonic() - t0
        print(f"  ✓ PASS  ({elapsed:.1f}s)")
        return True
    except FileNotFoundError:
        elapsed = time.monotonic() - t0
        print(f"  ✗ FAIL  (git or python not found) ({elapsed:.1f}s)")
        return False


def _stale_hook_warning() -> str | None:
    """Return a warning if the installed hook is not what we would install.

    The hook lives in ``.git/`` and is not version-controlled, so pulling a
    fix to ``scripts/install_hooks.py`` does not update it — someone has to
    re-run the installer, and nothing used to notice if they did not. That is
    how a hook known to corrupt ``FILEMAP.md`` from a linked worktree went on
    running in checkouts that already had the fix on disk.

    This file *is* version-controlled and runs on every commit, so it is the
    one place that can see the drift. Advisory only: a stale hook still works
    for the common case, and blocking the commit over it would be worse than
    the drift.
    """
    try:
        from scripts.install_hooks import HOOK_CONTENT, installed_hook_path
    except ImportError:
        return None

    hook = installed_hook_path()
    if hook is None:
        return None
    try:
        installed = hook.read_text(encoding="utf-8")
    except OSError:
        # No hook, or unreadable. Someone running this script directly is not
        # who this warning is for.
        return None
    if installed == HOOK_CONTENT:
        return None
    return (
        f"Installed pre-commit hook differs from scripts/install_hooks.py ({hook}).\n"
        "  Re-run:  python scripts/install_hooks.py --force"
    )


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_streams()

    parser = argparse.ArgumentParser(description="Run fast pre-commit checks.")
    parser.add_argument("--skip-fmt", action="store_true", help="Skip formatting checks.")
    args = parser.parse_args(argv)

    # Always regenerate the derived files first (and stage them).
    # Track results in the summary so failures are visible.
    results: list[tuple[str, bool, float]] = []
    for label, script, output in _GENERATED:
        t0 = time.monotonic()
        ok = _regenerate(label, script, output)
        results.append((label, ok, time.monotonic() - t0))

    checks = _CHECKS if not args.skip_fmt else [c for c in _CHECKS if c[0] not in _FMT_LABELS]

    print("=" * 60)
    print("Pre-commit checks")
    print("=" * 60)

    for label, argv_template in checks:
        cmd = _resolve_argv(argv_template)
        print(f"\n▶ {label}")
        t0 = time.monotonic()
        # ``go fmt`` runs against staged blob content rather than the working
        # tree so Windows ``core.autocrlf=true`` checkouts don't flag clean
        # commits as unformatted.  See ``_gofmt_staged_blobs``.
        if label == "go fmt":
            staged = _staged_go_files()
            if not staged:
                elapsed = time.monotonic() - t0
                print(f"  ✓ PASS  ({elapsed:.1f}s)  (no staged .go files)")
                results.append((label, True, elapsed))
                continue
            try:
                unformatted_paths = _gofmt_staged_blobs(staged)
            except FileNotFoundError:
                elapsed = time.monotonic() - t0
                print("  ✗ FAIL  (command not found: gofmt)")
                results.append((label, False, elapsed))
                continue
            elapsed = time.monotonic() - t0
            passed = not unformatted_paths
            if unformatted_paths:
                print("\n".join(unformatted_paths))
            status = "✓ PASS" if passed else "✗ FAIL"
            print(f"  {status}  ({elapsed:.1f}s)")
            results.append((label, passed, elapsed))
            continue
        try:
            proc = subprocess.run(cmd, cwd=REPO_ROOT)
        except FileNotFoundError:
            elapsed = time.monotonic() - t0
            print(f"  ✗ FAIL  (command not found: {cmd[0]})")
            results.append((label, False, elapsed))
            continue
        elapsed = time.monotonic() - t0
        passed = proc.returncode == 0
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}  ({elapsed:.1f}s)")
        results.append((label, passed, elapsed))

    # Summary
    total_time = sum(r[2] for r in results)
    failed = [r for r in results if not r[1]]

    print("\n" + "=" * 60)
    print(f"Results: {len(results) - len(failed)}/{len(results)} passed  ({total_time:.1f}s)")

    # After the results line so it is the last thing on screen on success, and
    # is not mistaken for one of the checks.
    stale = _stale_hook_warning()
    if stale:
        print(f"\n⚠ {stale}")

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
