"""EXP-001 from the command line: ``python -m evaluators.exp001 practice ROOT``.

``practice`` holds the practice series for each arm named, in the order
named, then judges the practice memos and writes the report
(:mod:`evaluators.exp001.practice`). Started again with the same *ROOT*, it
goes on where it stopped.

``--provider anthropic`` makes real model calls on the pre-registered
models, so it spends money: the advisers', arm A's and the judge's calls go
to Anthropic with the key in ``ANTHROPIC_API_KEY``. A key used by nothing
else keeps the provider's usage report for that key to the run's own calls,
which the report's totals are compared with. ``--provider offline`` holds
the meetings on the offline mock provider instead, at no cost, and judges
nothing. The provider is never a default.

The channel arms run the orchestrator binary: ``make build-orchestrator``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path

from agents.llm_client import LLMClient
from agents.llm_offline import MockProvider
from agents.llm_providers import AnthropicProvider
from evaluators.exp001 import practice, practice_report
from evaluators.exp001.attempts import HarnessFault
from evaluators.exp001.costs import ARMS
from evaluators.exp001.deployment import ARMS_ALIAS, REPO, Alias
from evaluators.exp001.judge import load_prompts
from evaluators.exp001.materials import load_materials
from evaluators.exp001.packets import load_adviser_names
from evaluators.exp001.panel import load_panel

MATERIALS = REPO / "evaluators" / "experiments" / "EXP-001"
BINARY = REPO / "bin" / "persatrix-server"
# Every model alias on the offline mock provider, which charges nothing.
OFFLINE = Alias("mock", "offline", 0, 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    arms = tuple(args.arms)
    if len(set(arms)) != len(arms):
        parser.error("name each arm once")
    if set(arms) - {"A"} and not args.binary.is_file():
        print(f"{args.binary} is missing: run `make build-orchestrator`", file=sys.stderr)
        return 2
    offline = args.provider == "offline"
    try:
        report = asyncio.run(practice.run_practice(
            args.root, arms,
            panel=load_panel(MATERIALS / "panel.yaml"),
            series=load_materials(MATERIALS).practice,
            names=load_adviser_names(MATERIALS / "panel.yaml"),
            client=LLMClient(MockProvider() if offline else AnthropicProvider()),
            binary=args.binary,
            alias=OFFLINE if offline else ARMS_ALIAS,
            prompts=None if offline else load_prompts(MATERIALS / "rubric.yaml"),
            progress=_progress,
        ))
    except HarnessFault as fault:
        print(f"harness fault: {fault}", file=sys.stderr)
        return 2
    except practice.RefusedError as refused:
        print(refused, file=sys.stderr)
        return 2
    print(practice_report.summary(report), end="")
    print(f"The report: {args.root / practice.REPORT}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluators.exp001",
        description="Run EXP-001's harness (docs/experiments/EXP-001-harness.md).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "practice", help="hold the practice series for each arm, judge it and report",
    )
    run.add_argument("root", type=Path, help="the practice run's directory, kept whole")
    run.add_argument(
        "--provider", required=True, choices=("anthropic", "offline"),
        help="anthropic spends money; offline holds the meetings on the mock provider",
    )
    run.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS),
                     help="the arms to hold, in order (default: all five)")
    run.add_argument("--binary", type=Path, default=BINARY, help="the orchestrator binary")
    return parser


def _progress(message: str) -> None:
    print(f"{dt.datetime.now(dt.UTC):%H:%M:%S} {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
