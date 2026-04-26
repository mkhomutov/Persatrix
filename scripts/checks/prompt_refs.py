#!/usr/bin/env python3
"""Check that every ``instructions_file`` reference in agents.yaml exists.

JSON-schema validation only checks that ``instructions_file`` is a string
matching ``^prompts/``; it does not verify the referenced markdown file
actually exists on disk.  A typo, rename, or accidental deletion of a
prompt file would therefore pass ``make validate`` and surface only at
agent-server startup as a ``SystemExit``.

This script walks every ``agents.yaml`` under ``config/``, collects all
``instructions_file`` references, and confirms each resolves to a regular
file inside the ``prompts/`` subtree.  It mirrors the deny-by-default
rule in ``agents.prompt_loader`` so the validate-time and runtime
contracts stay aligned.

Usage::

    python scripts/checks/prompt_refs.py [--verbose]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402

_PROMPTS_SUBDIR = "prompts"
_CONFIG_GLOB = "config/agents.yaml"


class BrokenRef(NamedTuple):
    config_file: str
    agent_id: str
    ref: str
    reason: str


def _iter_agent_configs(repo_root: Path) -> list[Path]:
    """Return all ``agents.yaml`` files under ``config/``.

    Today there is exactly one (``config/agents.yaml``), but the project
    plans per-environment splits in v0.3.x — globbing keeps the check
    valid as soon as those land without requiring a code change here.
    """
    config_dir = repo_root / "config"
    if not config_dir.is_dir():
        return []
    return sorted(config_dir.glob("agents.yaml")) + sorted(
        config_dir.glob("agents.*.yaml")
    )


def check_prompt_refs(repo_root: Path, verbose: bool = False) -> list[BrokenRef]:
    """Scan agent configs and return a list of broken prompt references."""
    prompts_root = (repo_root / _PROMPTS_SUBDIR).resolve()
    failures: list[BrokenRef] = []
    checked = 0
    config_files = _iter_agent_configs(repo_root)

    print("[SCAN] Checking instructions_file references...")

    for config_file in config_files:
        rel = config_file.relative_to(repo_root).as_posix()
        if verbose:
            print(f"  Checking: {rel}")

        try:
            data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            failures.append(BrokenRef(
                config_file=rel,
                agent_id="?",
                ref="",
                reason=f"could not parse YAML: {exc}",
            ))
            continue

        if not isinstance(data, dict):
            continue
        agents = data.get("agents")
        if not isinstance(agents, list):
            continue

        for agent in agents:
            if not isinstance(agent, dict):
                continue
            ref = agent.get("instructions_file")
            if ref is None:
                continue
            agent_id = str(agent.get("id", "?"))
            checked += 1

            if not isinstance(ref, str) or not ref.strip():
                failures.append(BrokenRef(
                    config_file=rel,
                    agent_id=agent_id,
                    ref=str(ref),
                    reason="instructions_file must be a non-empty string",
                ))
                continue

            # Mirror the runtime deny-by-default check so validate-time
            # behavior matches what the prompt loader will do at startup.
            candidate = (repo_root / ref).resolve()
            try:
                candidate.relative_to(prompts_root)
            except ValueError:
                failures.append(BrokenRef(
                    config_file=rel,
                    agent_id=agent_id,
                    ref=ref,
                    reason=f"resolves outside {prompts_root} (deny-by-default)",
                ))
                continue

            if not candidate.is_file():
                failures.append(BrokenRef(
                    config_file=rel,
                    agent_id=agent_id,
                    ref=ref,
                    reason=f"file not found: {candidate}",
                ))

    n = len(config_files)
    print(
        f"[OK] Checked {checked} reference(s) across "
        f"{n} config file{'s' if n != 1 else ''}"
    )
    return failures


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(
        description="Check instructions_file references in agents.yaml resolve to real files.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show each file being checked",
    )
    args = parser.parse_args(argv)

    failures = check_prompt_refs(REPO_ROOT, verbose=args.verbose)

    if failures:
        print()
        print(f"[FAIL] Found {len(failures)} broken reference(s):")
        print()
        for f in failures:
            print(f"  Config:   {f.config_file}")
            print(f"  Agent:    {f.agent_id}")
            print(f"  Ref:      {f.ref}")
            print(f"  Reason:   {f.reason}")
            print()
        return 1

    print("[OK] All instructions_file references resolve!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
