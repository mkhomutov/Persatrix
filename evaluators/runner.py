"""RFC 0044 Phase 1 — the eval runner + Makefile entry point (PR 3).

The runner is the orchestration half of Phase 1. For a recipe it:

1. builds the right LLM provider for the **mode** — ``replay`` plays a recorded
   golden (:class:`~evaluators.replay_llm_client.ReplayProvider`), ``record``
   wraps a live provider to capture one, ``drift`` runs live (RFC 0044 §C);
2. drives the recipe's interactions/turns through a :class:`PersonaDriver` to
   produce an observed :class:`~evaluators.assertions.EvalRun`;
3. calls :func:`~evaluators.eval_set.evaluate` and serializes a structured
   per-assertion artifact (:mod:`evaluators.report`, RFC 0044 §F).

The persona-runtime adapter lives in :mod:`evaluators.persona_driver` and is
imported **lazily** (only the CLI / record / drift paths need it), so
``import evaluators.runner`` and the pure orchestration below do not drag in the
``agents`` runtime — the same lightness contract as
:mod:`evaluators.replay_llm_client`. Orchestration is tested against a fake
:class:`PersonaDriver`; the real adapter is tested in ``test_eval_persona_driver``.

Phase 1 produces a report a human reads; a failed eval does not gate merge until
Phase 2 wires ``.github/workflows/eval.yml`` (RFC 0044 §F). The CLI already
returns a non-zero exit on failure so that gate is a one-line change later.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from evaluators.assertions import EvalRun
from evaluators.eval_set import EvalReport, EvalSet, evaluate, load_eval_set
from evaluators.replay_llm_client import (
    RecordingProvider,
    ReplayProvider,
    dump_cassette,
)
from evaluators.report import report_to_dict, suite_report, write_report

#: Default eval-set recipe directory. Empty/absent in Phase 1 — the seed recipes
#: and their ``.golden.yaml`` sidecars land in PR 4 (gated on RFC 0041 events).
DEFAULT_EVAL_SETS_DIR = "evaluators/eval_sets"
#: Default persona config the runner resolves ``setup.persona`` against.
DEFAULT_CONFIG_PATH = "config/agents.yaml"


class EvalMode(Enum):
    """How a recipe is run against a provider (RFC 0044 §C)."""

    REPLAY = "replay"  # recorded golden → deterministic, CI-safe
    RECORD = "record"  # wrap a live provider, capture the golden
    DRIFT = "drift"  # live run to detect the golden no longer matches reality


# ─── elapsed parsing (OQ #5) ─────────────────────────────────────────────────

_ELAPSED_RE = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_elapsed(spec: str) -> float:
    """Parse a simulated-``elapsed`` string (``5m``, ``2h``, ``1d``) to seconds.

    The schema already constrains the field to ``^[0-9]+(s|m|h|d)$``, but the
    runner re-validates so a direct (non-schema) caller fails loudly rather than
    silently injecting a zero delta. No existing codebase helper parses these —
    the reverse (:func:`agents.temporal.rendering.format_duration`) is
    seconds→prose — so the multipliers live here.
    """
    m = _ELAPSED_RE.fullmatch(spec) if isinstance(spec, str) else None
    if m is None:
        raise ValueError(
            f"invalid elapsed {spec!r}: expected <int><s|m|h|d> (e.g. '5m', '2h', '1d')"
        )
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2)]


# ─── the driver seam ─────────────────────────────────────────────────────────


@runtime_checkable
class PersonaDriver(Protocol):
    """Drives a recipe's interactions/turns against a provider → an ``EvalRun``.

    The real implementation (:class:`evaluators.persona_driver.PersonaRuntimeDriver`)
    wires the persona runtime; orchestration tests substitute a deterministic
    fake so the runner can be exercised without the runtime.
    """

    async def run(self, eval_set: EvalSet, provider: Any) -> EvalRun: ...


async def run_eval(eval_set: EvalSet, *, provider: Any, driver: PersonaDriver) -> EvalReport:
    """Drive ``eval_set`` through ``driver`` against ``provider`` and evaluate it."""
    run = await driver.run(eval_set, provider)
    return evaluate(eval_set, run)


# ─── recipe discovery + golden sidecar ───────────────────────────────────────


def golden_path_for(recipe_path: str | Path) -> Path:
    """The sidecar golden path for a recipe (OQ #1: ``<id>.golden.yaml``)."""
    p = Path(recipe_path)
    return p.with_name(f"{p.stem}.golden.yaml")


def discover_recipes(eval_sets_dir: str | Path, target: str | None = None) -> list[Path]:
    """Return recipe files under ``eval_sets_dir`` (``.golden.yaml`` excluded).

    A missing directory yields ``[]`` — Phase 1's ``eval_sets/`` is empty until
    PR 4 — so ``make eval-replay`` is a clean no-op rather than an error.
    ``target`` filters to a single recipe by stem (``EVAL-MEMORY-001``).
    """
    d = Path(eval_sets_dir)
    if not d.is_dir():
        return []
    recipes = sorted(p for p in d.glob("*.yaml") if not p.name.endswith(".golden.yaml"))
    if target is not None:
        recipes = [p for p in recipes if p.stem == target]
    return recipes


# ─── provider building per mode ──────────────────────────────────────────────


def build_provider(
    mode: EvalMode,
    *,
    golden_path: str | Path | None = None,
    agent_config: dict[str, Any] | None = None,
) -> Any:
    """Construct the ``LLMProvider`` for ``mode``.

    - ``replay`` → a :class:`ReplayProvider` bound to the recorded golden. A
      missing golden is a hard :class:`FileNotFoundError` (a recipe whose golden
      was never recorded must fail loudly, never silently pass — RFC 0044 §D).
    - ``record`` → a :class:`RecordingProvider` wrapping the recipe's live
      provider (built via the agents factory, lazily imported).
    - ``drift`` → the bare live provider.
    """
    if mode is EvalMode.REPLAY:
        if golden_path is None or not Path(golden_path).is_file():
            raise FileNotFoundError(
                f"no golden for replay at {golden_path!r} — record one with "
                f"`make eval-record TARGET=<id>` (RFC 0044 §C)"
            )
        return ReplayProvider.from_file(golden_path)

    # record / drift both need a real provider — lazy-import the agents factory so
    # `import evaluators.runner` stays free of the runtime.
    from agents.llm_factory import create_provider  # noqa: PLC0415

    if agent_config is None:
        raise ValueError(f"{mode.value} mode requires the persona's agent_config")
    provider, _physical_model = create_provider(agent_config)
    if mode is EvalMode.RECORD:
        return RecordingProvider(provider)
    return provider  # drift


# ─── running a recipe / a suite ──────────────────────────────────────────────


def _default_driver(config_path: str | Path) -> PersonaDriver:
    """Build the production persona-runtime driver (lazy — pulls in the runtime)."""
    from evaluators.persona_driver import (  # noqa: PLC0415
        PersonaRuntimeDriver,
        default_config_resolver,
    )

    return PersonaRuntimeDriver(config_resolver=default_config_resolver(config_path))


async def _run_recipe(
    recipe_path: Path,
    *,
    mode: EvalMode,
    driver: PersonaDriver,
    config_path: str | Path,
) -> tuple[EvalReport, EvalSet]:
    """Load one recipe, build the mode's provider, drive it, and evaluate.

    In ``record`` mode the captured cassette is written to the sidecar golden
    after the run (CI never overwrites a golden — this is the explicit author
    path, RFC 0044 §C).
    """
    eval_set = load_eval_set(recipe_path)
    golden = golden_path_for(recipe_path)

    if mode is EvalMode.REPLAY:
        provider = build_provider(EvalMode.REPLAY, golden_path=golden)
        report = await run_eval(eval_set, provider=provider, driver=driver)
        return report, eval_set

    # record / drift resolve the persona config for the live provider.
    from evaluators.persona_driver import default_config_resolver  # noqa: PLC0415

    agent_config = default_config_resolver(config_path)(eval_set.setup.persona)
    provider = build_provider(mode, agent_config=agent_config)
    report = await run_eval(eval_set, provider=provider, driver=driver)
    if mode is EvalMode.RECORD:
        dump_cassette(provider.cassette, golden)
    return report, eval_set


async def run_suite(
    recipes: list[Path],
    *,
    mode: EvalMode,
    driver: PersonaDriver | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    """Run every recipe and return the per-recipe artifact dicts."""
    drv = driver if driver is not None else _default_driver(config_path)
    out: list[dict[str, Any]] = []
    for recipe in recipes:
        report, eval_set = await _run_recipe(
            recipe, mode=mode, driver=drv, config_path=config_path
        )
        out.append(report_to_dict(report, tier=eval_set.tier, mode=mode))
    return out


# ─── CLI (`python -m evaluators.runner`, the make targets) ────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evaluators.runner",
        description="RFC 0044 golden-trace eval runner (replay / record / drift).",
    )
    parser.add_argument(
        "--mode", choices=[m.value for m in EvalMode], default=EvalMode.REPLAY.value
    )
    parser.add_argument("--target", help="run a single recipe by id (e.g. EVAL-MEMORY-001)")
    parser.add_argument("--eval-sets-dir", default=DEFAULT_EVAL_SETS_DIR)
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--report", help="write the structured JSON artifact to this path")
    return parser.parse_args(argv)


def _print_summary(suite: dict[str, Any]) -> None:
    for d in suite["evals"]:
        mark = "PASS" if d["passed"] else "FAIL"
        s = d["summary"]
        print(f"  [{mark}] {d['eval_id']} ({d['mode']}, {d['tier']}): "
              f"{s['passed']}/{s['total']} assertions")
        if not d["passed"]:
            for a in d["assertions"]:
                if not a["passed"]:
                    print(f"      ✗ {a['name']}: {a['detail']}")
    s = suite["summary"]
    print(f"eval suite: {s['passed']}/{s['evals']} recipes passed")


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m evaluators.runner`` and the make targets."""
    # Harden the summary print against a non-UTF-8 stdout: `_print_summary` emits a
    # `✗` (U+2717) on a failed-assertion line, which raises UnicodeEncodeError under
    # a non-UTF-8 encoding (Windows cp1252, or an explicit PYTHONIOENCODING=ascii /
    # latin-1) — turning a legitimate red into a crash + truncated output on the
    # `make eval-replay` path Phase 2 gates CI on. `errors="replace"` degrades the
    # glyph rather than aborting; the ASCII `[FAIL]` / assertion text is unaffected.
    # (A bare C/POSIX locale on Python ≥3.11 is already safe — PEP 540 UTF-8 mode
    # covers it — so this is the belt for the cases it does not.)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # non-reconfigurable stream (e.g. redirected)
        pass
    args = _parse_args(argv)
    mode = EvalMode(args.mode)
    recipes = discover_recipes(args.eval_sets_dir, args.target)
    if not recipes:
        which = f" matching {args.target!r}" if args.target else ""
        print(
            f"no eval sets{which} in {args.eval_sets_dir}/ — nothing to run. "
            f"(Seed recipes land in RFC 0044 PR 4, gated on RFC 0041 typed events.)"
        )
        return 0

    reports = asyncio.run(run_suite(recipes, mode=mode, config_path=args.config))
    suite = suite_report(reports)
    if args.report:
        write_report(args.report, suite)
    _print_summary(suite)
    # Replay/drift signal failure via exit code so Phase 2 can gate on it; record
    # is a capture step and succeeds as long as the run completed.
    if mode is EvalMode.RECORD:
        return 0
    return 0 if suite["summary"]["passed_all"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised via the make targets
    sys.exit(main())
