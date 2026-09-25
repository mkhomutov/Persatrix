"""Pin CI's Python installs to the committed constraints file.

CI used to install the agents' dependencies with a bare `pip install -e
".[dev]"`, which takes the newest release PyPI has at that minute. On
2026-09-25 OpenTelemetry 1.45.0 reached PyPI and three minutes later the
required Python job failed on every open PR, whatever the PR changed (#1002).
CI now installs at the versions in `.github/python-constraints.txt`, so a new
release reaches CI only through a reviewed change to that file.

These tests pin the parts that make that true: the file covers every
dependency `agents/pyproject.toml` declares, the one install command passes
it, no workflow installs around that command, and the required Python job
fails a PR whose file no longer matches `agents/pyproject.toml`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from _test_infra import makefile_recipe_body
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB = REPO_ROOT / ".github"
CONSTRAINTS = GITHUB / "python-constraints.txt"
WORKFLOWS = GITHUB / "workflows"

UV_PIN_RE = re.compile(r"^UV_VERSION\s*:=\s*(\d+\.\d+\.\d+)\s*$", re.M)
# A pin line: `name==version`, optionally followed by ` ; marker`.
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", re.M)
# A pip install of the agents package itself: its dev extra, the agents
# directory, an editable install, or `.` (the package in the working
# directory). Installing pip or a tool is fine.
AGENTS_INSTALL_RE = re.compile(
    r"pip3?\s+install\b[^\n]*?"
    r"(\.\[dev\]|\bagents\b|\s(?:-e|--editable)(?:\s|=)|\s\.(?:\s|$))",
    re.M,
)


def _makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _pins() -> dict[str, list[str]]:
    """Every pinned version per package; a platform split can pin one twice."""
    pins: dict[str, list[str]] = {}
    for name, version in PIN_RE.findall(CONSTRAINTS.read_text(encoding="utf-8")):
        pins.setdefault(canonicalize_name(name), []).append(version)
    return pins


def _declared() -> list[Requirement]:
    """What CI installs: the base dependencies plus the `dev` extra."""
    text = (REPO_ROOT / "agents" / "pyproject.toml").read_text(encoding="utf-8")
    project = tomllib.loads(text)["project"]
    specs = project["dependencies"] + project["optional-dependencies"]["dev"]
    return [Requirement(spec) for spec in specs]


def _job_steps(workflow: str, job: str) -> list[dict[str, object]]:
    """The ordered `steps` of one job in one workflow."""
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return list(doc["jobs"][job]["steps"])


def _run_scripts() -> list[tuple[str, str]]:
    """(`file:job`, script) for every `run:` step in every workflow (`.yml` or
    `.yaml`) and every composite action under `.github/actions/`."""
    paths = [*WORKFLOWS.glob("*.y*ml"), *GITHUB.glob("actions/**/action.y*ml")]
    scripts = []
    for path in sorted(paths):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # A composite action has one step list, under `runs`.
        jobs = doc.get("jobs") or {"runs": doc.get("runs") or {}}
        for job_id, job in jobs.items():
            for step in job.get("steps") or []:
                if "run" in step:
                    scripts.append((f"{path.relative_to(GITHUB)}:{job_id}", str(step["run"])))
    return scripts


def _installs_agents(script: str) -> bool:
    """Whether a `run:` script installs the agents package with pip. A
    backslash continuation is joined first, so a command split across lines
    is read as one."""
    return bool(AGENTS_INSTALL_RE.search(script.replace("\\\n", " ")))


def test_makefile_declares_exactly_one_uv_pin() -> None:
    pins = UV_PIN_RE.findall(_makefile())
    assert len(pins) == 1, f"expected one UV_VERSION := X.Y.Z pin, found {pins}"


def test_constraints_targets_refuse_an_unpinned_uv() -> None:
    """The check diffs uv's output byte for byte, so every target runs the pin."""
    makefile = _makefile()
    for target in ("python-constraints", "python-constraints-upgrade", "python-constraints-check"):
        rule = re.search(rf"^{re.escape(target)}:([^\n#]*)", makefile, re.M)
        assert rule, f"no `{target}` rule in the Makefile"
        assert "python-constraints-uv" in rule.group(1).split(), (
            f"`{target}` does not depend on the uv version check"
        )
    assert "$(UV_VERSION)" in makefile_recipe_body("python-constraints-uv")


def test_constraints_pin_every_declared_dependency_inside_its_range() -> None:
    """The offline half of `make python-constraints-check`: a dependency added
    to agents/pyproject.toml, or a range narrowed there, without re-resolving
    the file fails here with no network and no uv."""
    pins = _pins()
    problems = []
    for req in _declared():
        versions = pins.get(canonicalize_name(req.name))
        if not versions:
            problems.append(f"{req.name} is not pinned")
            continue
        outside = [v for v in versions if not req.specifier.contains(v, prereleases=True)]
        if outside:
            problems.append(f"{req.name} is pinned at {outside}, outside '{req.specifier}'")
    assert not problems, "run `make python-constraints`: " + "; ".join(problems)


def test_constraints_file_names_the_command_that_writes_it() -> None:
    header = CONSTRAINTS.read_text(encoding="utf-8").splitlines()[:2]
    assert header[0].startswith("# This file was autogenerated by uv"), header
    assert header[1].strip() == "#    make python-constraints", header


def test_build_agents_installs_at_the_pinned_versions() -> None:
    """`make build-agents` is the one install command, locally and in CI."""
    assert re.search(
        r"^PYTHON_CONSTRAINTS\s*:=\s*\.github/python-constraints\.txt\s*$", _makefile(), re.M
    ), "PYTHON_CONSTRAINTS does not name .github/python-constraints.txt"
    recipe = makefile_recipe_body("build-agents")
    install = [line for line in recipe.splitlines() if " install " in line]
    assert len(install) == 1, f"expected one pip install line in build-agents, found {install}"
    assert "-c ../$(PYTHON_CONSTRAINTS)" in install[0], "build-agents installs without the pins"


def test_no_workflow_installs_the_agents_package_around_the_pins() -> None:
    """A direct `pip install -e ".[dev]"` takes whatever PyPI has that minute."""
    offenders = [where for where, script in _run_scripts() if _installs_agents(script)]
    assert not offenders, f"install through `make build-agents` instead: {offenders}"


@pytest.mark.parametrize(
    "script",
    [
        'cd agents && pip install -e ".[dev]"',
        "pip install --editable .",
        "pip install --editable=.",
        "pip install .",
        "python -m pip install ./agents",
        'pip install \\\n  -e ".[dev]"',
    ],
)
def test_install_detector_catches_each_shape_of_an_agents_install(script: str) -> None:
    assert _installs_agents(script)


@pytest.mark.parametrize(
    "script",
    ["python -m pip install --upgrade pip", "pip install pre-commit", "make build-agents"],
)
def test_install_detector_passes_other_installs(script: str) -> None:
    assert not _installs_agents(script)


def test_uv_compile_ignores_the_callers_uv_settings() -> None:
    """The check compares uv's output byte for byte, so a local uv.toml or a
    `UV_*` index, cut-off date or strategy must not reach the resolution."""
    m = re.search(r"^UV_COMPILE\s*=((?:[^\n]*\\\n)*[^\n]*)", _makefile(), re.M)
    assert m, "no UV_COMPILE definition in the Makefile"
    command = m.group(1)
    assert "--no-config" in command, "UV_COMPILE reads uv.toml files"
    for var in ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXCLUDE_NEWER", "UV_RESOLUTION"):
        assert f"-u {var}" in command, f"UV_COMPILE does not clear {var}"


def test_uv_install_target_installs_the_pin() -> None:
    """One install command for CI and developers; --force replaces another uv."""
    recipe = makefile_recipe_body("uv-install")
    assert "--force" in recipe and "uv==$(UV_VERSION)" in recipe, recipe


@pytest.mark.parametrize(
    ("workflow", "job", "consumer"),
    [
        ("ci.yml", "python", "make python-constraints-check"),
        ("python-constraints-refresh.yml", "refresh", "make python-constraints-upgrade"),
    ],
)
def test_workflow_installs_the_pinned_uv_before_the_constraints(
    workflow: str, job: str, consumer: str
) -> None:
    """uv comes from the Makefile pin, and the constraints step runs before the
    install, so a stale file is the first red."""
    runs = [str(step.get("run", "")) for step in _job_steps(workflow, job)]

    def first(text: str) -> int:
        found = [i for i, run in enumerate(runs) if text in run]
        assert found, f"{workflow}:{job} has no step running `{text}`"
        return found[0]

    assert [run.strip() for run in runs].count("make uv-install") == 1, (
        f"{workflow}:{job} must install uv with exactly one `make uv-install` step"
    )
    assert not [run for run in runs if "uv==" in run], f"{workflow}:{job} hardcodes a uv version"
    assert first("make uv-install") < first(consumer) < first("make build-agents"), (
        f"{workflow}:{job} must install uv, then run `{consumer}`, then `make build-agents`"
    )


def test_python_job_runs_the_constraints_check_once() -> None:
    runs = [str(step.get("run", "")).strip() for step in _job_steps("ci.yml", "python")]
    assert runs.count("make python-constraints-check") == 1, (
        "the python job must run exactly `make python-constraints-check`"
    )


def test_refresh_builds_its_branch_from_main() -> None:
    """The issue's link compares against main, so the branch must sit on main
    even when the workflow is dispatched from another ref."""
    steps = _job_steps("python-constraints-refresh.yml", "refresh")
    checkout = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkout, "the refresh job has no checkout step"
    options = checkout[0].get("with")
    assert isinstance(options, dict) and options.get("ref") == "main", (
        "the refresh job must check out main"
    )
