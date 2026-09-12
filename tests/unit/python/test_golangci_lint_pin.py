"""Pin the Go lint gate (ISSUE-0142, v0.3.16 PR C1).

`make lint` used to run whichever golangci-lint the machine had, with no
config and no CI job — so "the Go leg is green" was neither portable nor
enforced. These tests pin the three parts that fix that: one version pin in
the Makefile, a committed config, and a CI step that installs that pin and
runs the same Make target a developer runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PIN_RE = re.compile(r"^GOLANGCI_LINT_VERSION\s*:=\s*(v\d+\.\d+\.\d+)\s*$", re.M)


def _makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _go_job_steps() -> list[dict[str, Any]]:
    workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return list(ci["jobs"]["go"]["steps"])


def test_makefile_declares_exactly_one_golangci_lint_pin() -> None:
    pins = PIN_RE.findall(_makefile())
    assert len(pins) == 1, f"expected one GOLANGCI_LINT_VERSION := vX.Y.Z pin, found {pins}"


def test_lint_go_runs_the_pinned_binary_not_whatever_is_installed() -> None:
    """The target refuses a binary whose version differs from the pin."""
    recipe = re.search(r"^lint-go:.*?\n((?:\t.*\n)+)", _makefile(), re.M | re.S)
    assert recipe is not None, "no lint-go recipe"
    body = recipe.group(1)
    assert "GOLANGCI_LINT_VERSION" in body, "lint-go does not compare against the pin"
    assert "golangci-lint-install" in body, "the mismatch message does not name the install target"


def test_ci_go_job_installs_the_pin_and_runs_make_lint_go() -> None:
    """CI reads the Makefile pin (one source of truth) and runs the same target."""
    steps = _go_job_steps()
    action = "golangci/golangci-lint-action@"
    install = [s for s in steps if str(s.get("uses", "")).startswith(action)]
    assert len(install) == 1, "the go job does not install golangci-lint via the action"
    with_ = install[0].get("with", {})
    assert with_.get("install-only") is True, "the action must only install; `make lint-go` runs it"
    reads_pin = [s for s in steps if "make -s golangci-lint-version" in str(s.get("run", ""))]
    assert len(reads_pin) == 1, "no step reads the pin with `make -s golangci-lint-version`"
    step_id = reads_pin[0].get("id")
    assert step_id, "the pin-reading step needs an id the install step can reference"
    assert f"steps.{step_id}.outputs.version" in str(with_.get("version")), (
        "the action's version must be the pin the Makefile step read"
    )
    runs = [s for s in steps if str(s.get("run", "")).strip() == "make lint-go"]
    assert len(runs) == 1, "the go job must run exactly `make lint-go`"


def test_committed_golangci_config_enables_linters_explicitly() -> None:
    """No config means the linter set drifts with the version; the set is stated."""
    path = REPO_ROOT / ".golangci.yml"
    assert path.is_file(), ".golangci.yml is not committed"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert str(cfg.get("version")) == "2"
    linters = cfg["linters"]
    assert linters.get("default") == "none", "enable linters by name, never a version's default set"
    expected = {"errcheck", "govet", "ineffassign", "staticcheck", "unused"}
    assert set(linters.get("enable", [])) >= expected
