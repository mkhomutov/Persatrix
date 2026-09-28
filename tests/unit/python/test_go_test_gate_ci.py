"""Pin the Go test gate: every Go package, through one Make target (ISSUE-0177).

The orchestrator's own tests under `cmd/` ran in no CI job for five months.
CI and `make test-go` each listed the package trees by hand, and neither list
named `cmd/`; the Go integration tests had been left out the same way until
they got a CI step of their own. `make test-go` now runs `go test ./...`, the
whole module, so a new test tree runs with no edit, and the required Go job
runs that target instead of a copy of its command, so the two cannot drift.
"""

from __future__ import annotations

from _test_infra import ci_job_steps, makefile_recipe_body


def _go_test_lines(body: str) -> list[list[str]]:
    return [line.split() for line in body.splitlines() if line.split()[:2] == ["go", "test"]]


def test_make_test_go_runs_every_go_package() -> None:
    """One `go test` over `./...`, with the race detector: no hand-made tree list."""
    runs = _go_test_lines(makefile_recipe_body("test-go"))
    assert len(runs) == 1, f"test-go must run exactly one `go test`, found {runs}"
    patterns = [arg for arg in runs[0] if arg.startswith("./")]
    assert patterns == ["./..."], f"test-go must test `./...`, not a list of trees: {patterns}"
    assert "-race" in runs[0], "test-go must run with -race"


def test_the_go_job_runs_make_test_go_and_no_copy_of_its_command() -> None:
    """CI runs the developer's target; a raw `go test` step is a second list that can drift."""
    runs = [str(s.get("run", "")).strip() for s in ci_job_steps("go")]
    assert runs.count("make test-go") == 1, "the go job must run exactly `make test-go`"
    copies = [r for r in runs if "go test" in r]
    assert not copies, f"the go job runs `go test` outside `make test-go`: {copies}"
