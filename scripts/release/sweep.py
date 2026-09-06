#!/usr/bin/env python3
"""Run the release checklist's §1 gates as one command and print the results table.

Release-prep PR 4 (final pre-tag verification) runs fifteen commands by hand
and types their outcomes into the execution report's "Structural / Automated
Gates" table. This script runs the same list, records pass/fail and duration,
and prints that table ready to paste — the judgement stays human, the typing
stops. The list is the checklist template's §1
(``docs/templates/RELEASE_CHECKLIST_TEMPLATE.md``); keep the two in step.

Usage::

    python scripts/release/sweep.py                 # dry run: print the plan
    python scripts/release/sweep.py --execute       # run every default gate
    python scripts/release/sweep.py --execute --only "make lint,make validate"
    python scripts/release/sweep.py --execute --skip "make test"
    python scripts/release/sweep.py --execute --include-optional   # + Docker smoke
    python scripts/release/sweep.py --execute --report /tmp/sweep.md

Dry-run by default: the full sweep takes 15–25 minutes (the Python unit
tree alone is ~5.5 min) and ``make ui`` overwrites a tracked file that the
sweep restores afterwards. Gates run through ``$SHELL`` from the repo root
with ``PYTHON=<repo .venv python>`` exported so ``make`` targets use the
environment that has the dev dependencies.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_TIMEOUT_S = 1800


@dataclass(frozen=True)
class Gate:
    name: str
    command: str
    note: str = ""
    default: bool = True  # False = opt-in (needs a daemon, spends time or money)


@dataclass(frozen=True)
class GateResult:
    gate: Gate
    ok: bool
    seconds: float
    tail: str


#: The checklist §1 list, in order. ``command`` is a shell line run from the repo root.
GATES: tuple[Gate, ...] = (
    Gate("make test", "make test", "four legs: go, python, agents, integration"),
    Gate("cargo test", "cd cli && cargo test", "incl. the CLI↔server lockstep guards"),
    Gate("make lint", "make lint", "golangci-lint, ruff + mypy, imports-check, clippy"),
    Gate("mypy tests", "mypy tests/", "the separate leg"),
    Gate("make validate", "make validate"),
    Gate(
        "proto in sync",
        "make proto && git diff --exit-code internal/generated/ agents/generated/"
        " && make proto-check",
    ),
    Gate("sanitizer sync", "make generate-sanitizer-patterns-check"),
    Gate(
        "ui build + test",
        "make ui && make ui-test && make ui-html-check; rc=$?; "
        "git checkout -- internal/ui/assets/index.html; exit $rc",
        "restores the tracked placeholder index.html afterwards",
    ),
    Gate("eval-replay", "make eval-replay", "all recipes must replay green"),
    Gate("licenses", "make check-licenses"),
    Gate("notices", "make notices-check", "a delta is expected when deps changed since the tag"),
    Gate("file size", "python scripts/checks/file_size.py --strict"),
    Gate(
        "doc gates",
        "python scripts/checks/doc_links.py && python scripts/checks/doc_status_markers.py"
        " && python scripts/checks/doc_leaked_markup.py"
        " && python scripts/generate_filemap.py --check"
        " && python scripts/merged_prs.py --check && python scripts/checks/plan_status.py",
    ),
    Gate("indexes", "make rfcs-check && make issues-check"),
    Gate("offline smoke", "make demo-autonomous", "Docker, $0; opt-in", default=False),
)


def select(
    gates: Iterable[Gate],
    *,
    only: list[str] | None,
    skip: list[str] | None,
    include_optional: bool,
) -> list[Gate]:
    chosen = [g for g in gates if g.default or include_optional]
    if only:
        wanted = [o.strip() for o in only]
        chosen = [g for g in chosen if g.name in wanted]
    if skip:
        unwanted = {s.strip() for s in skip}
        chosen = [g for g in chosen if g.name not in unwanted]
    return chosen


def _venv_python() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def shell_runner(timeout_s: int = DEFAULT_TIMEOUT_S) -> Callable[[Gate], GateResult]:
    """The real runner: one shell line per gate, output captured, tail kept on failure."""

    def run(gate: Gate) -> GateResult:
        env = dict(os.environ)
        python = _venv_python()
        env["PYTHON"] = python
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                gate.command, shell=True, cwd=REPO_ROOT, env=env,
                capture_output=True, encoding="utf-8", errors="replace", timeout=timeout_s,
            )
            ok = proc.returncode == 0
            out = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            ok = False
            partial = exc.stdout if isinstance(exc.stdout, str) else ""
            out = f"timed out after {timeout_s}s\n" + partial
        seconds = time.monotonic() - t0
        tail = "" if ok else "\n".join(out.strip().splitlines()[-15:])
        return GateResult(gate=gate, ok=ok, seconds=seconds, tail=tail)

    return run


def run_gates(gates: list[Gate], *, runner: Callable[[Gate], GateResult]) -> list[GateResult]:
    results: list[GateResult] = []
    for gate in gates:
        print(f"▶ {gate.name}: {gate.command}", flush=True)
        result = runner(gate)
        mark = "✓ pass" if result.ok else "✗ FAIL"
        print(f"  {mark}  ({result.seconds:.1f}s)", flush=True)
        results.append(result)
    return results


def render_table(results: list[GateResult]) -> str:
    lines = ["| Gate | Command | Result |", "|------|---------|--------|"]
    for r in results:
        verdict = f"✅ pass ({r.seconds:.1f}s)" if r.ok else f"❌ **FAIL** ({r.seconds:.1f}s)"
        cmd = r.gate.command.replace("|", "\\|")
        lines.append(f"| {r.gate.name} | `{cmd}` | {verdict} |")
    failed = [r for r in results if not r.ok]
    if failed:
        lines.append("")
        for r in failed:
            lines.append(f"**{r.gate.name}** — last lines:")
            lines.append("")
            lines.append("```")
            lines.append(r.tail)
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


def exit_code(results: list[GateResult]) -> int:
    return 1 if any(not r.ok for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the release checklist §1 gates.")
    parser.add_argument(
        "--execute", action="store_true", help="run the gates (default: print the plan)",
    )
    parser.add_argument("--only", help="comma-separated gate names to run")
    parser.add_argument("--skip", help="comma-separated gate names to skip")
    parser.add_argument(
        "--include-optional", action="store_true", help="also run opt-in gates (Docker smoke)",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="per-gate timeout in seconds",
    )
    parser.add_argument("--report", type=Path, help="write the results table to this file")
    args = parser.parse_args(argv)

    gates = select(
        GATES,
        only=args.only.split(",") if args.only else None,
        skip=args.skip.split(",") if args.skip else None,
        include_optional=args.include_optional,
    )
    if not args.execute:
        print("Dry run — pass --execute to run. The sweep would run, in order:\n")
        for g in gates:
            note = f"  — {g.note}" if g.note else ""
            print(f"  {g.name:<18} {g.command}{note}")
        skipped = [g.name for g in GATES if not g.default and not args.include_optional]
        if skipped:
            print(f"\nOpt-in (add --include-optional): {', '.join(skipped)}")
        return 0

    results = run_gates(gates, runner=shell_runner(args.timeout))
    table = render_table(results)
    print("\n" + table)
    if args.report:
        args.report.write_text(table, encoding="utf-8")
        print(f"wrote {args.report}")
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
