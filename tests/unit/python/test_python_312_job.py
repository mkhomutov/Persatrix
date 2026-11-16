"""Hold the CI job that runs the unit suite on the agent image's Python (ISSUE-0219).

The agent image runs Python 3.12 and every CI job ran 3.11, so no check ran
the agents' code on the Python that ships. One job now does. These tests keep
what makes that job mean something:

- it runs on the Python the image is built on, read from ``Dockerfile.agent``,
  so the two cannot part;
- every other job stays on 3.11, the oldest Python the package supports,
  read from ``agents/pyproject.toml``;
- it installs from the pins by the one install command, and runs the whole
  unit tree with nothing left out, by the same target a developer runs;
- it runs on every pull request and fails when a test fails;
- it is named as not required, where the required checks are versioned.

Files are read as text and YAML; nothing here installs or runs the suite.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

from _test_infra import REPO_ROOT, ci_workflow, makefile_recipe_body

JOB = "python-312"
JOB_NAME = "Python 3.12 (unit tests)"
INSTALL = "make build-agents"
RUN_TESTS = "make test-python"
# Options that make pytest leave out part of the tree it is given.
NARROWING = re.compile(r"(?:^|\s)(?:-[kmx]\b|--(?:ignore|deselect|maxfail|lf|ff|co)\b)")


def _job(name: str = JOB) -> dict[str, Any]:
    return dict(ci_workflow()["jobs"][name])


def _runs(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]).strip() for step in job["steps"] if "run" in step]


def _python_of(job: dict[str, Any]) -> list[str]:
    """Each `python-version` a job's set-up steps name."""
    return [
        str(step["with"]["python-version"])
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]


def _image_python() -> str:
    """The Python the agent image is built on: `FROM python:<major.minor>-slim`."""
    text = (REPO_ROOT / "Dockerfile.agent").read_text(encoding="utf-8")
    found = re.findall(r"^FROM python:(\d+\.\d+)(?:\.\d+)?-", text, re.M)
    assert len(found) == 1, f"expected one `FROM python:` line in Dockerfile.agent, found {found}"
    return str(found[0])


def _oldest_supported_python() -> str:
    with (REPO_ROOT / "agents" / "pyproject.toml").open("rb") as handle:
        requires = str(tomllib.load(handle)["project"]["requires-python"])
    found = re.fullmatch(r">=(\d+\.\d+)", requires)
    assert found, f"requires-python is {requires!r}; this test reads `>=X.Y`"
    return found.group(1)


def test_the_job_runs_on_the_python_the_agent_image_is_built_on() -> None:
    assert _python_of(_job()) == [_image_python()], (
        "the job exists to test the Python that ships: move it with Dockerfile.agent"
    )
    assert _image_python() != _oldest_supported_python(), (
        "the image is on the oldest supported Python, which every other job tests: "
        "this job has become a second run of the same thing"
    )


def test_its_name_says_the_python_it_runs() -> None:
    assert _job()["name"] == JOB_NAME
    assert _image_python() in JOB_NAME


def test_every_other_job_stays_on_the_oldest_supported_python() -> None:
    """3.11 is the floor the package declares. A job moved off it would leave
    the floor untested, however many jobs run a newer Python."""
    floor = _oldest_supported_python()
    others = {
        name: _python_of(dict(spec))
        for name, spec in ci_workflow()["jobs"].items()
        if name != JOB
    }
    with_python = {name: versions for name, versions in others.items() if versions}
    assert len(with_python) >= 5, f"too few jobs set Python up to judge: {sorted(with_python)}"
    for name, versions in with_python.items():
        assert versions == [floor], f"job {name!r} runs Python {versions}, not {floor}"


def test_it_installs_from_the_pins_by_the_one_install_command() -> None:
    runs = _runs(_job())
    installs = [run for run in runs if "install" in run]
    assert len(installs) == 1, f"one install step, found {installs}"
    lines = [line.strip() for line in installs[0].splitlines() if line.strip()]
    assert lines == ["python -m pip install --upgrade pip", INSTALL]
    recipe = makefile_recipe_body("build-agents")
    assert "-c $(PYTHON_CONSTRAINTS)" in recipe, "build-agents no longer installs from the pins"


def test_it_runs_the_whole_unit_tree_by_the_developers_target() -> None:
    runs = _runs(_job())
    assert runs[-1] == RUN_TESTS, f"the last step runs the suite: {runs}"
    install = next(run for run in runs if INSTALL in run)
    assert runs.index(install) < runs.index(RUN_TESTS), runs
    recipe = makefile_recipe_body("test-python").strip()
    assert recipe.startswith("$(PYTHON) -m pytest tests/unit/python/"), recipe
    options = recipe.split("pytest", 1)[1]  # what pytest is given, not python's own -m
    assert not NARROWING.search(options), f"test-python leaves part of the tree out: {recipe}"
    # The 3.11 job runs the same tree with the same options, so a difference
    # between the two results is the Python's.
    unit_step = [r for r in _runs(_job("python")) if "pytest tests/unit/python/" in r]
    assert unit_step == [recipe.replace("$(PYTHON)", "python")], unit_step


def test_it_runs_on_every_pull_request_and_a_failed_test_fails_it() -> None:
    job = _job()
    assert "if" not in job and not job.get("continue-on-error")
    assert "needs" not in job, "it does not wait for the 3.11 job"
    for step in job["steps"]:
        assert "if" not in step and not step.get("continue-on-error"), step
        assert "PYTEST_ADDOPTS" not in (step.get("env") or {}), step
    assert "PYTEST_ADDOPTS" not in {**(ci_workflow().get("env") or {}), **(job.get("env") or {})}


def test_it_is_named_as_not_required() -> None:
    """Which checks `main` requires is the owner's setting. The job is in the
    versioned file as not required, with a reason, and not among the required."""
    config = json.loads(
        (REPO_ROOT / "docs" / "methodology" / "branch-protection.json").read_text(encoding="utf-8")
    )
    assert JOB_NAME not in config["contexts"]
    assert JOB_NAME in config["not_required"]
