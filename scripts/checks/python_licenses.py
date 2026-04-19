#!/usr/bin/env python3
"""Third-party Python dependency license check.

Runs ``pip-licenses`` against the currently-installed environment and fails
if any dependency declares a license outside the canonical allow-list in
``scripts/checks/allowed_licenses.txt``.

The check is deliberately strict: multi-license declarations (e.g.
``MIT; BSD-3-Clause``) are accepted only if *every* component is allowed,
because a library licensed under ``MIT OR GPL-3.0`` still lets a downstream
user pick GPL-3.0, but our concern is the set of licenses actually
compatible with BUSL-1.1 distribution. If a package genuinely ships dual-
licensed with at least one allowed option, add it to ``--exception`` with
a comment explaining why.

Usage::

    python scripts/checks/python_licenses.py
    python scripts/checks/python_licenses.py --exception Persatrix-agents

The script expects ``pip-licenses`` to be installed (it is in the
``dev`` extra of ``agents/pyproject.toml``).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALLOWED_FILE = REPO_ROOT / "scripts" / "checks" / "allowed_licenses.txt"

# pip-licenses separates multi-license strings with these delimiters.
_SPLIT_RE = re.compile(r"\s*(?:;|,| OR | AND )\s*", re.IGNORECASE)


def load_allowed_licenses(path: Path) -> set[str]:
    allowed: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            allowed.add(line)
    return allowed


def normalize(license_str: str) -> list[str]:
    """Split a pip-licenses license field into individual SPDX-ish tokens.

    Some packages (notably tiktoken) put the full license text into the
    ``License:`` metadata field. The first line is conventionally the license
    name (e.g. ``MIT License``), so strip to that before splitting.
    """
    if not license_str or license_str.upper() == "UNKNOWN":
        return ["UNKNOWN"]
    first_line = license_str.strip().splitlines()[0].strip()
    parts = [p.strip() for p in _SPLIT_RE.split(first_line) if p.strip()]
    return parts or [first_line]


def run_pip_licenses() -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "piplicenses",
                "--format=json",
                "--from=mixed",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print(
            "error: pip-licenses not installed. Run `cd agents && pip install -e \".[dev]\"`.",
            file=sys.stderr,
        )
        sys.exit(2)
    except subprocess.CalledProcessError as exc:
        print(f"error: pip-licenses failed: {exc.stderr}", file=sys.stderr)
        sys.exit(2)
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exception",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="Skip license enforcement for this package (repeat for multiple). "
        "Use only with a justification in a review comment.",
    )
    parser.add_argument(
        "--allowed-file",
        type=Path,
        default=ALLOWED_FILE,
        help=f"Path to allow-list (default: {ALLOWED_FILE.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args()

    allowed = load_allowed_licenses(args.allowed_file)
    exceptions = {pkg.lower() for pkg in args.exception}

    violations: list[tuple[str, str, str]] = []
    checked = 0
    for pkg in run_pip_licenses():
        name = pkg.get("Name", "")
        version = pkg.get("Version", "")
        license_field = pkg.get("License", "UNKNOWN")
        if name.lower() in exceptions:
            continue
        checked += 1
        tokens = normalize(license_field)
        if any(tok not in allowed for tok in tokens):
            violations.append((name, version, license_field))

    if violations:
        print(f"License check FAILED — {len(violations)} disallowed package(s):", file=sys.stderr)
        for name, version, lic in violations:
            print(f"  {name} {version}: {lic}", file=sys.stderr)
        print(
            "\nTo resolve: replace the dependency, upgrade to a compatible "
            "version, or add a reviewed exception in the calling Makefile "
            "target / CI step.",
            file=sys.stderr,
        )
        return 1

    print(f"License check OK — {checked} packages, all licenses in allow-list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
