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

Phase 2 (RFC 0044 §F, v0.3.16 PR C2) gates merge on this runner: the required
Python CI job runs ``make eval-replay TIER=stable`` and the exit code is the
gate. ``--tier`` scopes a run to one declared tier. Every way a run can be
empty-handed is red, never green: a selection that matches no recipe exits 1,
and a recipe in the selection that cannot be loaded or has no recorded golden
is a *failed recipe in the report* (``recipe.load`` / ``golden.missing``), so
the summary, the ``--report`` artifact and the other recipes' verdicts survive.
A malformed recipe *outside* the selected tier is skipped unread — a draft
cannot block every merge merely by existing.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from evaluators.assertions import AssertionResult, EvalRun
from evaluators.eval_set import (
    DEFAULT_TIER,
    TIERS,
    EvalReport,
    EvalSet,
    evaluate,
    load_eval_set,
    parse_eval_set,
)
from evaluators.replay_llm_client import (
    RecordingProvider,
    ReplayProvider,
    dump_cassette,
)
from evaluators.report import report_to_dict, suite_report, write_report

#: Default eval-set recipe directory: the committed seed recipes and their
#: ``.golden.yaml`` sidecars (six as of v0.3.16, all ``tier: stable``).
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


async def run_eval_observed(
    eval_set: EvalSet, *, provider: Any, driver: PersonaDriver
) -> tuple[EvalReport, EvalRun]:
    """Drive ``eval_set`` and return both the evaluation and the observed run.

    The run rides along so the suite path can thread its captured
    ``shadow_traces`` (RFC 0049 PR 2) into the report artifact — the
    PR 4 shadow→live measurement input.
    """
    run = await driver.run(eval_set, provider)
    return evaluate(eval_set, run), run


async def run_eval(eval_set: EvalSet, *, provider: Any, driver: PersonaDriver) -> EvalReport:
    """Drive ``eval_set`` through ``driver`` against ``provider`` and evaluate it."""
    report, _run = await run_eval_observed(eval_set, provider=provider, driver=driver)
    return report


# ─── recipe discovery + golden sidecar ───────────────────────────────────────


def golden_path_for(recipe_path: str | Path) -> Path:
    """The sidecar golden path for a recipe (OQ #1: ``<id>.golden.yaml``)."""
    p = Path(recipe_path)
    return p.with_name(f"{p.stem}.golden.yaml")


def discover_recipes(eval_sets_dir: str | Path, target: str | None = None) -> list[Path]:
    """Return recipe files under ``eval_sets_dir``.

    Only ``EVAL-*.yaml`` files are recipes (the schema's closed id domain);
    golden sidecars (``*.golden.yaml``) and support fixtures living beside
    the recipes (``offline_responses.eval.yaml``) are excluded — the
    no-target ``make eval-replay`` sweep must not load a fixture as a
    recipe. A missing directory yields ``[]``; ``main`` treats an empty result
    as red, whatever the cause. ``target`` filters to a single recipe by stem
    (``EVAL-MEMORY-001``).
    """
    d = Path(eval_sets_dir)
    if not d.is_dir():
        return []
    recipes = sorted(
        p for p in d.glob("EVAL-*.yaml") if not p.name.endswith(".golden.yaml")
    )
    if target is not None:
        recipes = [p for p in recipes if p.stem == target]
    return recipes


@dataclass(frozen=True)
class LoadedRecipe:
    """A recipe read from disk once: its path (for the golden sidecar) + parse."""

    path: Path
    eval_set: EvalSet


@dataclass(frozen=True)
class LoadFailure:
    """A recipe in the selection that could not be loaded.

    ``tier`` is what the file declares (``DEFAULT_TIER`` when it declares none),
    or ``None`` when the file is not a YAML mapping at all — such a file has no
    knowable tier, so it is in every selection and always fails.
    """

    path: Path
    tier: str | None
    detail: str


def _read_mapping(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"eval-set must be a mapping, got {type(data).__name__}")
    return data


def load_recipes(
    recipes: Sequence[Path], tier: str | None = None
) -> tuple[list[LoadedRecipe], list[LoadFailure]]:
    """Load, once each, the recipes in ``tier`` (every recipe when ``None``).

    RFC 0044 §F: the CI gate runs ``--tier stable``, and a fresh recipe
    defaults to ``experimental`` — so a draft cannot block every merge merely
    by existing. That holds even for a *malformed* draft: the tier is peeked
    from the raw YAML and a recipe outside the selection is never validated.
    A recipe inside it that fails to load comes back as a :class:`LoadFailure`
    (the report's ``recipe.load`` row), never as an exception, so one bad file
    cannot take the other recipes' verdicts and the artifact down with it.
    """
    loaded: list[LoadedRecipe] = []
    failed: list[LoadFailure] = []
    for path in recipes:
        try:
            data = _read_mapping(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            failed.append(LoadFailure(path, None, str(exc)))
            continue
        declared = data.get("tier", DEFAULT_TIER)
        if tier is not None and declared != tier:
            continue
        try:
            loaded.append(LoadedRecipe(path, parse_eval_set(data)))
        except ValueError as exc:
            failed.append(LoadFailure(path, str(declared), str(exc)))
    return loaded, failed


def declared_tiers(recipes: Sequence[Path]) -> Counter[str]:
    """How many discovered recipes declare each tier (for the empty-tier message)."""
    tiers: Counter[str] = Counter()
    for path in recipes:
        try:
            tiers[str(_read_mapping(path).get("tier", DEFAULT_TIER))] += 1
        except (OSError, ValueError, yaml.YAMLError):
            tiers["<unreadable>"] += 1
    return tiers


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


def _failed_report(eval_id: str, *, name: str, detail: str) -> EvalReport:
    """A one-row failed report for a recipe that never ran (RFC 0044 §D: loud)."""
    return EvalReport(eval_id=eval_id, results=[AssertionResult(name, False, detail)])


def failed_artifact(
    eval_id: str, *, tier: str, mode: Any, name: str, detail: str
) -> dict[str, Any]:
    """The artifact dict for a recipe that could not be run at all."""
    report = _failed_report(eval_id, name=name, detail=detail)
    return report_to_dict(report, tier=tier, mode=mode)


async def _run_recipe(
    recipe: Path | LoadedRecipe,
    *,
    mode: EvalMode,
    driver: PersonaDriver,
    config_path: str | Path,
) -> tuple[EvalReport, EvalSet, EvalRun]:
    """Build the mode's provider for one recipe, drive it, and evaluate.

    A bare ``Path`` is loaded here (raising on a malformed recipe — the
    programmatic callers' choice); ``main`` passes :class:`LoadedRecipe` so
    nothing is parsed twice. In ``record`` mode the captured cassette is written
    to the sidecar golden after the run (CI never overwrites a golden — this is
    the explicit author path, RFC 0044 §C).
    """
    if isinstance(recipe, LoadedRecipe):
        loaded = recipe
    else:
        loaded = LoadedRecipe(recipe, load_eval_set(recipe))
    eval_set = loaded.eval_set
    golden = golden_path_for(loaded.path)

    if mode is EvalMode.REPLAY:
        try:
            provider = build_provider(EvalMode.REPLAY, golden_path=golden)
        except FileNotFoundError as exc:
            # A never-recorded golden fails loudly (§D) — as a failed recipe in
            # the artifact, not an exception out of the suite: the summary, the
            # report and the other recipes' verdicts must survive it.
            report = _failed_report(eval_set.id, name="golden.missing", detail=str(exc))
            return report, eval_set, EvalRun(turn_outputs=[], terminal_state={}, events=[])
        report, run = await run_eval_observed(eval_set, provider=provider, driver=driver)
        return report, eval_set, run

    # record / drift resolve the persona config for the live provider.
    from evaluators.persona_driver import default_config_resolver  # noqa: PLC0415

    agent_config = default_config_resolver(config_path)(eval_set.setup.persona)
    provider = build_provider(mode, agent_config=agent_config)
    report, run = await run_eval_observed(eval_set, provider=provider, driver=driver)
    if mode is EvalMode.RECORD:
        dump_cassette(provider.cassette, golden)
    return report, eval_set, run


async def run_suite(
    recipes: Sequence[Path | LoadedRecipe],
    *,
    mode: EvalMode,
    driver: PersonaDriver | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    """Run every recipe and return the per-recipe artifact dicts."""
    drv = driver if driver is not None else _default_driver(config_path)
    out: list[dict[str, Any]] = []
    for recipe in recipes:
        report, eval_set, run = await _run_recipe(
            recipe, mode=mode, driver=drv, config_path=config_path
        )
        out.append(
            report_to_dict(
                report, tier=eval_set.tier, mode=mode,
                shadow_traces=run.shadow_traces,
            )
        )
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
    parser.add_argument(
        "--tier", choices=TIERS, help="run only the recipes declared in this tier (CI: stable)"
    )  # TIERS is the schema's enum — a misspelt tier is an argparse error, not a vacuous run
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
    # One rule for every empty-handed run: a run that replays nothing exits 1.
    # A step that passes on nothing is the vacuous green RFC 0044 §F forbids,
    # and the developer's `TARGET=<typo>` is the same hole on the other side.
    vacuous = "a run over nothing is vacuous, not green (RFC 0044 §F)"
    which = f" matching {args.target!r}" if args.target else ""
    recipes = discover_recipes(args.eval_sets_dir, args.target)
    if not recipes:
        print(f"no eval sets{which} in {args.eval_sets_dir}/ — {vacuous}")
        return 1
    loaded, failed = load_recipes(recipes, tier=args.tier)
    if not loaded and not failed:
        # Say what the discovered recipes DO declare, so a target that merely
        # sits in another tier — or a wrong tier — is diagnosed, not "empty".
        found = ", ".join(f"{t} ×{n}" for t, n in sorted(declared_tiers(recipes).items()))
        print(
            f"no recipes in tier {args.tier!r}{which} under {args.eval_sets_dir}/ — "
            f"the {len(recipes)} found declare {found}; {vacuous}"
        )
        return 1

    reports = asyncio.run(run_suite(loaded, mode=mode, config_path=args.config))
    reports += [
        failed_artifact(
            f.path.stem, tier=f.tier or "<unknown>", mode=mode, name="recipe.load", detail=f.detail
        )
        for f in failed
    ]
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
