#!/usr/bin/env python3
"""Show or apply the versioned required-status-check set for ``main``.

The set lives in ``docs/methodology/branch-protection.json`` so a change to it
is a reviewed diff. ``--show`` reads the live setting through ``gh api`` and
prints what differs; ``--apply`` PATCHes the file's set (repository admin
required — the owner runs it). Nothing else about branch protection
(linear history, PR required, force-push blocked) is touched: those are
stable and are documented in the enforcement matrix.

Usage::

    python scripts/release/branch_protection.py --show
    python scripts/release/branch_protection.py --apply
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_streams  # noqa: E402

CONFIG = REPO_ROOT / "docs" / "methodology" / "branch-protection.json"
REPO = "mkhomutov/Persatrix"
ENDPOINT = f"repos/{REPO}/branches/main/protection/required_status_checks"


def load_desired(path: Path = CONFIG) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"strict": bool(data["strict"]), "contexts": sorted(data["contexts"])}


def diff(live: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    """Human-readable differences; empty when the live setting matches the file."""
    out: list[str] = []
    if live.get("strict") != desired["strict"]:
        out.append(f"strict: live={live.get('strict')} file={desired['strict']}")
    live_ctx = set(live.get("contexts") or [])
    want_ctx = set(desired["contexts"])
    for name in sorted(want_ctx - live_ctx):
        out.append(f"+ required by file, not live: {name}")
    for name in sorted(live_ctx - want_ctx):
        out.append(f"- live, not in file: {name}")
    return out


def _gh(*args: str, payload: str | None = None) -> str:
    cmd = ["gh", "api", *args]
    proc = subprocess.run(
        cmd, input=payload, capture_output=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed: {' '.join(args)}")
    return proc.stdout


def show(desired: dict[str, Any]) -> int:
    live = json.loads(_gh(ENDPOINT))
    live_view = {"strict": live.get("strict"), "contexts": sorted(live.get("contexts") or [])}
    lines = diff(live_view, desired)
    if not lines:
        print(
            f"live setting matches {CONFIG.relative_to(REPO_ROOT)} "
            f"({len(desired['contexts'])} contexts, strict={desired['strict']})"
        )
        return 0
    print("differences (file is the intended state):")
    for line in lines:
        print(f"  {line}")
    print("apply with: make branch-protection-apply")
    return 1


def apply(desired: dict[str, Any]) -> int:
    payload = json.dumps({"strict": desired["strict"], "contexts": desired["contexts"]})
    _gh("-X", "PATCH", ENDPOINT, "--input", "-", payload=payload)
    print(f"applied {len(desired['contexts'])} required contexts (strict={desired['strict']})")
    return show(desired)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_streams()
    parser = argparse.ArgumentParser(description="Show or apply main's required status checks.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--show", action="store_true", help="diff the file against the live setting")
    mode.add_argument(
        "--apply", action="store_true", help="PATCH the live setting to the file (admin)",
    )
    args = parser.parse_args(argv)
    desired = load_desired()
    try:
        return apply(desired) if args.apply else show(desired)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
