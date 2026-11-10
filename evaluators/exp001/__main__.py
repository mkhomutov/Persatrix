"""EXP-001 from the command line: ``python -m evaluators.exp001 practice ROOT``,
then ``python -m evaluators.exp001 scored ROOT``, then
``python -m evaluators.exp001 score ROOT``.

``practice`` holds the practice series for each arm named, in the order
named, then judges the practice memos and writes the report
(:mod:`evaluators.exp001.practice`). ``scored`` holds the scored run: every
arm's scored series in its drawn order, then the scored judging and the
report (:mod:`evaluators.exp001.scored`). Started again with the same
*ROOT*, either goes on where it stopped. After a harness fault, the scored
run's next window starts only with ``--fixed-by`` naming the reviewed PR
that fixed it; after a provider error the judge's retries did not clear, a
plain start resumes the scored judging.

``--provider anthropic`` makes real model calls on the pre-registered
models, so it spends money: the advisers', arm A's and the judge's calls go
to Anthropic with the key in ``ANTHROPIC_API_KEY``. A key used by nothing
else keeps the provider's usage report for that key to the run's own calls,
which the report's totals are compared with. ``--provider offline`` holds
the meetings on the offline mock provider instead, at no cost, and judges
nothing. The provider is never a default.

The channel arms run the orchestrator binary: ``make build-orchestrator``.

``score`` scores a scored run, or a practice run, once its packets are
drawn and judged (:mod:`evaluators.exp001.scoring_run`). It writes each
person a blank score sheet the first time and says how many packets each
rater has left; with ``--open``, once every score is in, it opens the seal
and writes the result. It makes no model call.

``rater-batches``, ``rater-enter`` and ``rater-differences`` are for the AI
raters who score in the two people's place
(:mod:`evaluators.exp001.ai_raters`): a rater's session writes its rater's
batches as prompts, enters each run's answer into the rater's sheet, and,
on a practice run, lists the cells the two raters scored differently.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from agents.llm_client import LLMClient
from agents.llm_offline import MockProvider
from agents.llm_providers import AnthropicProvider
from evaluators.exp001 import (
    ai_raters,
    practice,
    practice_report,
    result_report,
    scored,
    scored_report,
    scoring_run,
)
from evaluators.exp001.attempts import HarnessFault
from evaluators.exp001.costs import ARMS
from evaluators.exp001.deployment import ARMS_ALIAS, REPO, Alias
from evaluators.exp001.judge import JudgePrompts, load_prompts
from evaluators.exp001.materials import load_materials
from evaluators.exp001.packets import load_adviser_names
from evaluators.exp001.panel import load_panel
from evaluators.exp001.rating import PEOPLE
from evaluators.exp001.sheets import SheetError

MATERIALS = REPO / "evaluators" / "experiments" / "EXP-001"
BINARY = REPO / "bin" / "persatrix-server"
# Every model alias on the offline mock provider, which charges nothing.
OFFLINE = Alias("mock", "offline", 0, 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "scored":
        return _scored(args)
    if args.command == "score":
        return _score(args)
    if args.command.startswith("rater-"):
        return _rater(args)
    arms = tuple(args.arms)
    if len(set(arms)) != len(arms):
        parser.error("name each arm once")
    if set(arms) - {"A"} and not args.binary.is_file():
        print(f"{args.binary} is missing: run `make build-orchestrator`", file=sys.stderr)
        return 2
    materials = load_materials(MATERIALS)
    client, alias, prompts = _provider(args.provider)
    try:
        report = asyncio.run(practice.run_practice(
            args.root, arms,
            panel=load_panel(MATERIALS / "panel.yaml"),
            series=materials.practice,
            scored=materials.series,
            names=load_adviser_names(MATERIALS / "panel.yaml"),
            client=client,
            binary=args.binary,
            alias=alias,
            prompts=prompts,
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


def _scored(args: argparse.Namespace) -> int:
    """The scored run; once a harness fault, or a provider error the judge's
    retries did not clear, stops it, what the report written as it stopped says."""
    if not args.binary.is_file():
        print(f"{args.binary} is missing: run `make build-orchestrator`", file=sys.stderr)
        return 2
    client, alias, prompts = _provider(args.provider)
    try:
        report = asyncio.run(scored.run_scored(
            args.root,
            panel=load_panel(MATERIALS / "panel.yaml"),
            series=load_materials(MATERIALS).series,
            names=load_adviser_names(MATERIALS / "panel.yaml"),
            client=client,
            binary=args.binary,
            alias=alias,
            prompts=prompts,
            fixed_by=args.fixed_by,
            progress=_progress,
        ))
    except (HarnessFault, scored.JudgingStopped) as stop:
        print(f"harness fault: {stop}" if isinstance(stop, HarnessFault) else stop,
              file=sys.stderr)
        written = args.root / scored.REPORT
        if written.is_file():
            print(scored_report.summary(json.loads(written.read_text())), end="")
        return 2
    except scored.RefusedError as refused:
        print(refused, file=sys.stderr)
        return 2
    print(scored_report.summary(report), end="")
    print(f"The report: {args.root / scored.REPORT}")
    return 0


def _score(args: argparse.Namespace) -> int:
    """How far scoring has got, or, the seal open, the result."""
    try:
        scores = scoring_run.score(
            args.root, materials=load_materials(MATERIALS),
            prompts=load_prompts(MATERIALS / "rubric.yaml"), open_seal=args.open_seal,
        )
    except (scoring_run.ScoringRefused, SheetError, HarnessFault) as refused:
        print(refused, file=sys.stderr)
        return 2
    if isinstance(scores, scoring_run.Progress):
        print(result_report.progress_words(scores), end="")
        if args.open_seal:
            print("the seal stays shut: every score is not in yet", file=sys.stderr)
            return 2
        return 0
    print(result_report.summary(scores), end="")
    print(f"The result: {args.root / scoring_run.RESULT}")
    return 0


def _rater(args: argparse.Namespace) -> int:
    """One step of an AI rater's session; what the rater rules refuse fails."""
    try:
        if args.command == "rater-batches":
            for path in ai_raters.write_batches(
                args.directory, args.person, args.out, rubric=MATERIALS / "rubric.yaml",
                notes=args.notes,
            ):
                print(path)
        elif args.command == "rater-enter":
            entered = ai_raters.enter(args.directory, args.person, args.prompt, args.record,
                                      model=args.model)
            print(f"entered {len(entered)} packets from {args.prompt.stem}")
        else:
            listed = ai_raters.differences(args.root)
            print("\n".join(listed) if listed else "the two raters agree on every cell")
    except ai_raters.RaterRefused as refused:
        print(refused, file=sys.stderr)
        return 2
    return 0


def _provider(name: str) -> tuple[LLMClient, Alias, JudgePrompts | None]:
    """The client, model alias and judge's prompts either run is held with on
    the provider *name*: offline, the mock provider's alias, and no judging."""
    if name == "offline":
        return LLMClient(MockProvider()), OFFLINE, None
    return LLMClient(AnthropicProvider()), ARMS_ALIAS, load_prompts(MATERIALS / "rubric.yaml")


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
    held = commands.add_parser(
        "scored", help="hold the scored run: every arm's scored series, judged and reported",
    )
    held.add_argument("root", type=Path,
                      help="the scored run's directory, kept whole, every window in it")
    held.add_argument(
        "--provider", required=True, choices=("anthropic", "offline"),
        help="anthropic spends money; offline rehearses the run on the mock provider",
    )
    held.add_argument("--binary", type=Path, default=BINARY, help="the orchestrator binary")
    held.add_argument(
        "--fixed-by", metavar="PR",
        help="after a harness fault, the reviewed PR that fixed it: starts the next window",
    )
    scoring = commands.add_parser(
        "score", help="score a run's packets: open the seal once every score is in",
    )
    scoring.add_argument("root", type=Path, help="the scored or practice run's directory")
    scoring.add_argument(
        "--open", dest="open_seal", action="store_true",
        help="once every score is in, open the seal and write the result",
    )
    batches = commands.add_parser(
        "rater-batches", help="write an AI rater's packets as one prompt per batch",
    )
    batches.add_argument("directory", type=Path,
                         help="the folder holding the packets and the raters' files")
    batches.add_argument("person", choices=PEOPLE, help="whose sheet the rater fills")
    batches.add_argument("out", type=Path, help="where the prompts go, outside the run")
    batches.add_argument("--notes", type=Path,
                         help="the notes agreed in practice; none for the practice itself")
    entering = commands.add_parser(
        "rater-enter", help="enter an AI rater's answer, read from its run's record",
    )
    entering.add_argument("directory", type=Path,
                          help="the folder holding the packets and the raters' files")
    entering.add_argument("person", choices=PEOPLE, help="whose sheet the rater fills")
    entering.add_argument("prompt", type=Path, help="the batch's prompt")
    entering.add_argument("record", type=Path,
                          help="the run's own record, which holds its answer")
    entering.add_argument("--model", required=True,
                          help="the exact model ID the rater runs on; any other is refused")
    compared = commands.add_parser(
        "rater-differences", help="list the cells two AI raters scored differently in practice",
    )
    compared.add_argument("root", type=Path, help="the practice run's directory")
    return parser


def _progress(message: str) -> None:
    print(f"{dt.datetime.now(dt.UTC):%H:%M:%S} {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
