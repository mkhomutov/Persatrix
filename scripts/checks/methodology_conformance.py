#!/usr/bin/env python3
"""Check a checkout against the methodology's contract (docs/methodology/conformance.json).

The methodology is a set of documents, tools, make targets and CI steps. A
repository that adopts it — this one, or one that vendors the blueprint —
drifts by deleting a template, dropping a make target, or leaving a check out
of the CI job. This script reads the manifest and reports what is missing,
so drift is a red check on the PR that caused it rather than an audit
months later.

Usage::

    python scripts/checks/methodology_conformance.py [--manifest PATH] [--verbose]

Exit code: 0 conformant, 1 with the missing items listed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402

MANIFEST = REPO_ROOT / "docs" / "methodology" / "conformance.json"
_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+):", re.M)
_JOB_NAME_RE = re.compile(r"^\s{4}name: (.+)$", re.M)


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_targets(makefile_text: str) -> set[str]:
    return set(_TARGET_RE.findall(makefile_text)) - {".PHONY"}


def ci_job_names(ci_text: str) -> set[str]:
    return set(_JOB_NAME_RE.findall(ci_text))


def _docs_hygiene_block(ci_text: str) -> str:
    """The text of the ``docs-hygiene`` job, or "" when the job is absent."""
    m = re.search(r"^  docs-hygiene:\n(.*?)(?=^  [a-z-]+:\n|\Z)", ci_text, re.M | re.S)
    return m.group(1) if m else ""


def find_missing(repo_root: Path, manifest: dict[str, Any]) -> list[str]:
    """Every manifest entry the checkout lacks, as ``kind: item`` lines."""
    missing: list[str] = []
    for kind in ("documents", "tooling", "persatrix_specific"):
        for rel in manifest.get(kind, []):
            if not (repo_root / rel).is_file():
                missing.append(f"{kind}: {rel}")

    makefile = repo_root / "Makefile"
    targets = make_targets(makefile.read_text(encoding="utf-8")) if makefile.is_file() else set()
    for target in manifest.get("make_targets", []):
        if target not in targets:
            missing.append(f"make target: {target}")

    ci = repo_root / ".github" / "workflows" / "ci.yml"
    ci_text = ci.read_text(encoding="utf-8") if ci.is_file() else ""
    jobs = ci_job_names(ci_text)
    for job in manifest.get("ci_jobs", []):
        if job not in jobs:
            missing.append(f"ci job: {job}")
    hygiene = _docs_hygiene_block(ci_text)
    for step in manifest.get("ci_steps_in_docs_hygiene", []):
        if step not in hygiene:
            missing.append(f"docs-hygiene step: {step}")
    return missing


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Check the checkout against the methodology manifest.",
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    counted = sum(
        len(manifest.get(k, []))
        for k in ("documents", "tooling", "persatrix_specific", "make_targets", "ci_jobs",
                  "ci_steps_in_docs_hygiene")
    )
    missing = find_missing(REPO_ROOT, manifest)
    print(f"[SCAN] Checked {counted} methodology artifact(s) from {args.manifest.name}")
    if missing:
        print(f"\n[FAIL] {len(missing)} missing:")
        for line in missing:
            print(f"  {line}")
        print("\nAdd the artifact, or remove it from the manifest in the same PR with the reason.")
        return 1
    if args.verbose:
        for k in ("documents", "tooling"):
            for rel in manifest.get(k, []):
                print(f"  ok {rel}")
    print("[OK] The checkout carries every artifact the methodology names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
