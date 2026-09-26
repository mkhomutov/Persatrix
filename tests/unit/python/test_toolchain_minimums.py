"""Hold every Go, Python and Rust minimum the docs state to what the build requires.

Nothing compared the two, so they drifted apart. README said Go 1.24+ while
go.mod had required Go 1.25 since April (#100) and Go 1.26 since #877, and it
said Rust 1.80+ while the CLI's dependencies needed Rust 1.86. Each minimum
now has one home that the build itself reads: the `go` line in go.mod,
`requires-python` in agents/pyproject.toml and `rust-version` in
cli/Cargo.toml. These tests fail when a doc states anything else. The Rust
number is also proved rather than assumed: the rust CI job compiles the CLI
with exactly that release.

Checked are the places that tell a person what to install: README's badges and
quick start, CONTRIBUTING, the guides, the manual-test template and the manual
tests, the bug form, and the CLI's own "Install Python" message. Left alone, on
purpose, are records of what was true when they were written (execution
reports, the CHANGELOG, RFCs, issues) and comments that name the release a
feature arrived in ("Go 1.22+ pattern routing"). Versions compare at
major.minor, the precision the docs use.
"""

from __future__ import annotations

import functools
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[3]
BUG_FORM = ".github/ISSUE_TEMPLATE/bug_report.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Where minimums are stated. Globs, so a new guide or manual test is checked
# without anyone editing this list.
STATING_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "docs/guides/*.md",
    "docs/templates/*.md",
    "docs/manual-tests/MT-*.md",
    ".github/ISSUE_TEMPLATE/*.yml",
    "cli/src/commands/validate.rs",  # tells a user without Python which one to install
)
# "Go 1.26+" in prose, "Go-1.26%2B" in a shields.io badge URL.
STATED_RE = re.compile(r"\b(Go|Python|Rust)[ -](\d+)\.(\d+)(?:\.\d+)?(?:\+|%2B)")
# The bug form's example environment shows each tool at its minimum, with no "+".
EXAMPLE_RE = re.compile(r"\b(Go|Python|Rust) (\d+)\.(\d+)")
# `cargo "+$v" check …`: rustup's `+toolchain` override, taken from a shell variable.
CHECK_ON_RE = re.compile(r'cargo\s+"?\+\$\{?(\w+)\}?"?\s+check\b([^\n]*)')

# Files that must go on stating each minimum. The scan checks only what it
# finds, so a statement reworded past STATED_RE, or a file that moved, would
# otherwise pass unchecked. README is held separately: badge and quick start.
MUST_STATE = {
    "Go": (
        "CONTRIBUTING.md",
        "docs/guides/v0.3.0-demo.md",
        "docs/templates/MANUAL_TEST_TEMPLATE.md",
        BUG_FORM,
    ),
    "Python": (
        "CONTRIBUTING.md",
        "docs/guides/v0.3.0-demo.md",
        "docs/templates/MANUAL_TEST_TEMPLATE.md",
        BUG_FORM,
        "cli/src/commands/validate.rs",
    ),
    "Rust": (
        "docs/guides/v0.3.0-demo.md",
        BUG_FORM,
    ),
}


@dataclass(frozen=True)
class Statement:
    """One place that tells a person which release of a tool to install."""

    path: str  # relative to the repo root
    where: str  # what a failure prints: `path:line`, or the form field
    tool: str  # Go, Python or Rust
    version: tuple[int, int]
    # "badge" is the shields.io URL, which draws the badge; "badge text" is the
    # badge's alt text, shown only when the image fails; "prose" is the rest.
    form: str


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _go() -> tuple[int, int]:
    m = re.search(r"^go\s+(\d+)\.(\d+)", _read("go.mod"), re.M)
    assert m, "go.mod has no `go` line"
    return int(m.group(1)), int(m.group(2))


def _python() -> tuple[int, int]:
    spec = tomllib.loads(_read("agents/pyproject.toml"))["project"]["requires-python"]
    floors = [Version(s.version).release for s in SpecifierSet(spec) if s.operator == ">="]
    assert len(floors) == 1 and len(floors[0]) >= 2, (
        f"requires-python {spec!r} has no single `>=X.Y` floor"
    )
    return floors[0][0], floors[0][1]


def _rust() -> tuple[int, int]:
    declared = str(tomllib.loads(_read("cli/Cargo.toml"))["package"].get("rust-version", ""))
    m = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", declared)
    assert m, (
        "cli/Cargo.toml declares no `rust-version`, so the oldest Rust the CLI "
        "supports has no home that cargo, clippy and CI read"
    )
    return int(m.group(1)), int(m.group(2))


# Each tool's minimum: where it lives, and how to read it.
REQUIRED: dict[str, tuple[str, Callable[[], tuple[int, int]]]] = {
    "Go": ("the `go` line in go.mod", _go),
    "Python": ("requires-python in agents/pyproject.toml", _python),
    "Rust": ("rust-version in cli/Cargo.toml", _rust),
}


@functools.cache
def _statements() -> tuple[Statement, ...]:
    found = []
    for pattern in STATING_FILES:
        for path in sorted(REPO_ROOT.glob(pattern)):
            relative = path.relative_to(REPO_ROOT).as_posix()
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, start=1):
                for m in STATED_RE.finditer(line):
                    version = (int(m.group(2)), int(m.group(3)))
                    if m.group(0).endswith("%2B"):
                        form = "badge"
                    else:
                        form = "badge text" if "img.shields.io" in line else "prose"
                    where = f"{relative}:{number}"
                    found.append(Statement(relative, where, m.group(1), version, form))
    form = yaml.safe_load(_read(BUG_FORM))
    environment = next(field for field in form["body"] if field.get("id") == "environment")
    where = f"{BUG_FORM} (the Environment example)"
    for m in EXAMPLE_RE.finditer(str(environment["attributes"]["placeholder"])):
        version = (int(m.group(2)), int(m.group(3)))
        found.append(Statement(BUG_FORM, where, m.group(1), version, form="prose"))
    return tuple(found)


@pytest.mark.parametrize("tool", sorted(REQUIRED))
def test_every_stated_minimum_is_the_one_the_build_requires(tool: str) -> None:
    home, read = REQUIRED[tool]
    required = read()
    # dict.fromkeys: a badge line states its version twice, in the text and the URL.
    wrong = dict.fromkeys(
        f"{s.where} says {tool} {s.version[0]}.{s.version[1]}"
        for s in _statements()
        if s.tool == tool and s.version != required
    )
    assert not wrong, (
        f"{home} says {tool} {required[0]}.{required[1]}, but:\n  "
        + "\n  ".join(wrong)
        + "\nFix the docs, or the build's minimum if the docs are the ones that are right."
    )


@pytest.mark.parametrize("tool", sorted(MUST_STATE))
def test_the_setup_docs_still_state_each_minimum(tool: str) -> None:
    stating = {s.path for s in _statements() if s.tool == tool}
    missing = [path for path in MUST_STATE[tool] if path not in stating]
    assert not missing, (
        f"found no {tool} minimum in {missing}. Reworded past STATED_RE, or moved? "
        "Either way it is no longer checked"
    )


@pytest.mark.parametrize("tool", sorted(REQUIRED))
def test_readme_states_each_minimum_in_its_badge_and_its_quick_start(tool: str) -> None:
    forms = {s.form for s in _statements() if s.path == "README.md" and s.tool == tool}
    assert {"badge", "prose"} <= forms, (
        f"README.md must state the {tool} minimum in its badge's URL and in the "
        f"quick start; found only {sorted(forms)}"
    )


def test_ci_compiles_the_cli_on_its_rust_version() -> None:
    """Only a build with the `rust-version` release proves the number the docs
    state. A dependency update can raise the floor without touching our code,
    and clippy's MSRV lint sees the std APIs our code calls, not language
    features or what the dependencies need."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    matches = [
        (step, m)
        for step in workflow["jobs"]["rust"]["steps"]
        if (m := CHECK_ON_RE.search(str(step.get("run", ""))))
    ]
    assert len(matches) == 1, 'the rust job must run `cargo "+$<rust-version>" check` once'
    step, m = matches[0]
    run, variable, args = str(step["run"]), m.group(1), m.group(2).split()
    assert step.get("working-directory") == "cli", "run it in cli/, next to Cargo.toml"
    assert {"--locked", "--all-targets"} <= set(args), (
        "check the dependency versions CI builds (`--locked`), tests included (`--all-targets`)"
    )
    assert re.search(rf"\b{variable}=\$\(cargo metadata\b[^\n]*\brust_version\b", run), (
        f"${variable} must be rust-version, read from cli/Cargo.toml with `cargo metadata`"
    )
    assert not re.search(r"\b\d+\.\d+\b", run), (
        "the step must not name a Rust version of its own: rust-version is the one home"
    )
