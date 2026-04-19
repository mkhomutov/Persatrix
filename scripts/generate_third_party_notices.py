#!/usr/bin/env python3
"""Generate THIRD_PARTY_NOTICES.md for Go, Python, and Rust dependencies.

Runs the per-language license tooling already used by ``make check-licenses``
(``go-licenses``, ``pip-licenses``, ``cargo-license``) and assembles a single
human-readable notices file at the repository root.

Output convention: one section per language, each with a table of
``name | version | license | source``. For packages whose license is outside
the canonical allow-list in ``scripts/checks/allowed_licenses.txt`` the row
is still emitted but the license cell is prefixed with ``!`` so a reviewer
spots it during release prep.

The script does not fail the build on disallowed licenses — that is the
existing ``check-licenses-*`` targets' job. This script's job is to produce
the notices document.

Usage::

    python scripts/generate_third_party_notices.py               # write file
    python scripts/generate_third_party_notices.py --check       # diff only
    python scripts/generate_third_party_notices.py -o custom.md  # alt path
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_FILE = REPO_ROOT / "scripts" / "checks" / "allowed_licenses.txt"
DEFAULT_OUTPUT = REPO_ROOT / "THIRD_PARTY_NOTICES.md"

# Packages that ARE Persatrix itself — skip in the notices file.
SELF_NAMES = {
    "github.com/mkhomutov/persatrix",
    "persatrix-agents",
    "orch",
}

_OR_RE = re.compile(r"\s+OR\s+", re.IGNORECASE)
_AND_RE = re.compile(r"\s+AND\s+|\s*;\s*|\s*,\s*")
_PAREN_RE = re.compile(r"^\((.*)\)$")


def load_allowed_licenses() -> set[str]:
    allowed: set[str] = set()
    for raw in ALLOWED_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            allowed.add(line)
    return allowed


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0].strip() if text else ""


def _strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    m = _PAREN_RE.match(expr)
    if not m:
        return expr
    inner = m.group(1)
    depth = 0
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return expr
    return inner.strip() if depth == 0 else expr


def _split_top_level(expr: str, pattern: re.Pattern[str]) -> list[str]:
    """Split on the given operator pattern, ignoring text inside parentheses."""
    depth = 0
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if depth == 0:
            m = pattern.match(expr, i)
            if m:
                parts.append("".join(buf).strip())
                buf = []
                i = m.end()
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def is_allowed(license_str: str, allowed: set[str]) -> bool:
    """Evaluate a (simplified) SPDX expression against the allow-list.

    Rules:
      - ``A OR B`` passes if *any* operand passes.
      - ``A AND B`` passes only if *all* operands pass.
      - Parentheses group sub-expressions; bare tokens match the allow-list
        by exact string comparison.
    """

    expr = _first_line(license_str) or license_str or ""
    expr = expr.strip()
    if not expr:
        return False
    expr = _strip_outer_parens(expr)

    or_parts = _split_top_level(expr, _OR_RE)
    if len(or_parts) > 1:
        return any(is_allowed(p, allowed) for p in or_parts)

    and_parts = _split_top_level(expr, _AND_RE)
    if len(and_parts) > 1:
        return all(is_allowed(p, allowed) for p in and_parts)

    return expr in allowed


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    return result.stdout


def collect_go() -> list[dict[str, str]]:
    out = _run(
        [
            "go-licenses",
            "report",
            "./cmd/...",
            "./internal/...",
            "--ignore=github.com/mkhomutov/persatrix",
        ],
        cwd=REPO_ROOT,
    )
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[0]:
            continue
        name, url, lic = parts[0], parts[1], parts[2]
        if name.lower() in SELF_NAMES:
            continue
        rows.append({"name": name, "version": "", "license": lic, "source": url})
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def collect_python() -> list[dict[str, str]]:
    out = _run(
        [
            sys.executable,
            "-m",
            "piplicenses",
            "--format=json",
            "--from=mixed",
            "--with-urls",
        ],
    )
    data = json.loads(out)
    rows: list[dict[str, str]] = []
    for pkg in data:
        name = pkg.get("Name", "")
        if name.lower() in SELF_NAMES:
            continue
        rows.append(
            {
                "name": name,
                "version": pkg.get("Version", ""),
                "license": _first_line(pkg.get("License", "UNKNOWN")) or "UNKNOWN",
                "source": pkg.get("URL", "") or "",
            }
        )
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def collect_rust() -> list[dict[str, str]]:
    cli_dir = REPO_ROOT / "cli"
    out = _run(["cargo", "license", "--json"], cwd=cli_dir)
    data = json.loads(out)
    rows: list[dict[str, str]] = []
    for pkg in data:
        name = pkg.get("name", "")
        if name.lower() in SELF_NAMES:
            continue
        rows.append(
            {
                "name": name,
                "version": pkg.get("version", ""),
                "license": pkg.get("license", "") or pkg.get("license_file", "") or "UNKNOWN",
                "source": pkg.get("repository", "") or "",
            }
        )
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def _fmt_source(src: str) -> str:
    if not src:
        return ""
    if src.startswith(("http://", "https://")):
        return f"[link]({src})"
    return src


def _fmt_license(lic: str, allowed: set[str]) -> str:
    clean = lic.replace("|", "/").strip() or "UNKNOWN"
    return clean if is_allowed(clean, allowed) else f"!{clean}"


def render_table(rows: list[dict[str, str]], allowed: set[str], include_version: bool) -> str:
    if not rows:
        return "_No dependencies detected._\n"
    header = "| Package | Version | License | Source |\n| --- | --- | --- | --- |\n"
    if not include_version:
        header = "| Package | License | Source |\n| --- | --- | --- |\n"
    lines = [header]
    for r in rows:
        name = r["name"].replace("|", "/")
        lic = _fmt_license(r["license"], allowed)
        src = _fmt_source(r["source"])
        if include_version:
            lines.append(f"| `{name}` | {r['version']} | {lic} | {src} |\n")
        else:
            lines.append(f"| `{name}` | {lic} | {src} |\n")
    return "".join(lines)


def build_document(
    go_rows: list[dict[str, str]],
    py_rows: list[dict[str, str]],
    rs_rows: list[dict[str, str]],
    allowed: set[str],
) -> str:
    parts = [
        "# Third-Party Notices\n",
        "\n",
        "Persatrix bundles and redistributes third-party software. This file lists\n",
        "every dependency pulled into a Persatrix build, together with its license\n",
        "and source location.\n",
        "\n",
        "The file is **generated** — do not edit by hand. Regenerate with:\n",
        "\n",
        "```bash\n",
        "make notices\n",
        "```\n",
        "\n",
        "Policy:\n",
        "\n",
        "- Allow-list of acceptable licenses lives in\n",
        "  [`scripts/checks/allowed_licenses.txt`](scripts/checks/allowed_licenses.txt).\n",
        "  CI enforces the same list via `make check-licenses` (Go + Python + Rust).\n",
        "- Any row prefixed with `!` denotes a license *outside* the allow-list — a\n",
        "  reviewer must resolve it (replace the dependency, upgrade, or add a\n",
        "  justified exception) before release.\n",
        "- Persatrix itself ships under BUSL-1.1 (see [`LICENSE`](LICENSE) and\n",
        "  [`NOTICE`](NOTICE)) and is excluded from the tables below.\n",
        "\n",
        "## Go dependencies\n",
        "\n",
        f"Collected via `go-licenses report ./cmd/... ./internal/...` ({len(go_rows)} packages).\n",
        "\n",
        render_table(go_rows, allowed, include_version=False),
        "\n",
        "## Python dependencies\n",
        "\n",
        f"Collected via `pip-licenses --from=mixed` against the `agents` extras"
        f" ({len(py_rows)} packages).\n",
        "\n",
        render_table(py_rows, allowed, include_version=True),
        "\n",
        "## Rust dependencies\n",
        "\n",
        f"Collected via `cargo license --json` inside `cli/` ({len(rs_rows)} crates).\n",
        "\n",
        render_table(rs_rows, allowed, include_version=True),
    ]
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if regenerated content differs from --output.",
    )
    args = parser.parse_args()

    allowed = load_allowed_licenses()

    try:
        go_rows = collect_go()
        py_rows = collect_python()
        rs_rows = collect_rust()
    except FileNotFoundError as exc:
        print(
            f"error: required tool not on PATH ({exc.filename}). "
            "Install go-licenses, pip-licenses, and cargo-license before running.",
            file=sys.stderr,
        )
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc.cmd[0]} failed:\n{exc.stderr}", file=sys.stderr)
        return 2

    document = build_document(go_rows, py_rows, rs_rows, allowed)

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != document:
            print(
                f"THIRD_PARTY_NOTICES out of date at {args.output}. "
                "Regenerate with `make notices`.",
                file=sys.stderr,
            )
            return 1
        print(f"THIRD_PARTY_NOTICES up to date ({args.output}).")
        return 0

    args.output.write_text(document, encoding="utf-8")
    print(
        f"Wrote {args.output} — "
        f"{len(go_rows)} Go, {len(py_rows)} Python, {len(rs_rows)} Rust."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
