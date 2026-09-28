"""Fail when a test file sits where no CI step runs it (ISSUE-0178).

Five test trees in this repository ran in no job while their lint and type
checks stayed green: `agents/tests/` for 180 days, the Rust suite, the Python
integration tier, the Go integration tests and the orchestrator's tests under
`cmd/` (docs/methodology/testing-strategy.md, "The one rule that history paid
for"). Go's half of the rule has been structural since ISSUE-0177: `make
test-go` tests `./...`, and test_go_test_gate_ci.py pins that. These tests
hold the rest. Every tracked test file must sit where a CI step runs it:
under a root the Python job runs pytest on, inside the web console's Vitest
`include`, or in the CLI crate the Rust job runs `cargo test` in; and a Go
test file must not sit where `./...` cannot reach. Beside each language, a
pin fails when its CI step stops running that place.

A new test tree is added to the constants below in the same PR as its CI
step, so a tree left without a runner is a visible, reviewed diff. Files and
configs are read as text; nothing here calls pytest, Vitest, cargo or Go.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import PurePosixPath

import pytest
from _test_infra import REPO_ROOT, ci_job_steps, makefile_recipe_body

#: The trees the Python job runs pytest on, one step each, as the Makefile does.
PYTEST_ROOTS = ("tests/unit/python", "agents/tests", "tests/integration")
#: The web console: its Vitest config, whose `include` globs are relative to it.
WEB = "web"
#: The CLI crate: `cargo test` run in it builds and runs every test in it.
CRATE = "cli"

# pytest's default `python_files`; nothing in the repository overrides it.
PYTHON_TEST = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
# pytest's default `norecursedirs`: it collects nothing below these.
PYTEST_SKIPS = re.compile(r"\..*|.*\.egg|_darcs|build|CVS|dist|node_modules|venv|\{arch\}")
# Options that make pytest leave out part of a tree it is given.
NARROWING = re.compile(r"\s(?:-k|-m|--ignore|--ignore-glob|--deselect)(?:[\s=]|$)")
# Vitest's default test-file names, `**/*.{test,spec}.?(c|m)[jt]s?(x)`.
JS_TEST = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
# A Rust file that holds tests: a test module or a test function.
RUST_TEST = re.compile(r"#\[(?:cfg\(test\)|(?:tokio::)?test\b)")


def _git_ls_files(*pathspecs: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [name for name in out.split("\0") if name]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _runs(job: str) -> list[str]:
    return [str(step.get("run", "")).strip() for step in ci_job_steps(job)]


def _within(root: str, path: str) -> bool:
    return PurePosixPath(root) in PurePosixPath(path).parents


# ─── Python: the pytest roots ────────────────────────────────────────────────


def collected_by_pytest(root: str, path: str) -> bool:
    """True when `pytest <root>` collects the test file `path`."""
    if not _within(root, path):
        return False
    below = PurePosixPath(path).parent.parts[len(PurePosixPath(root).parts):]
    return not any(PYTEST_SKIPS.fullmatch(name) for name in below)


def outside_pytest(files: Iterable[str], roots: Iterable[str]) -> list[str]:
    """The Python test files no root's pytest run collects, sorted."""
    roots = list(roots)
    return sorted(f for f in files if not any(collected_by_pytest(r, f) for r in roots))


def _python_test_files() -> list[str]:
    return [f for f in _git_ls_files("*.py") if PYTHON_TEST.search(f)]


def test_every_python_test_file_is_under_a_root_ci_runs() -> None:
    files = _python_test_files()
    assert files, "found no Python test files, so this check would pass on nothing"
    stray = outside_pytest(files, PYTEST_ROOTS)
    assert not stray, (
        f"no CI step runs these Python test files: {stray}. Move them under a pytest "
        f"root ({', '.join(PYTEST_ROOTS)}), or give their tree a CI step and a make "
        "target and add it to PYTEST_ROOTS."
    )


def test_the_python_job_and_the_makefile_run_pytest_on_every_root() -> None:
    runs = _runs("python")
    makefile = _read("Makefile")
    for root in PYTEST_ROOTS:
        command = re.compile(rf"\bpytest {re.escape(root)}/?(?:\s|$)")
        steps = [run for run in runs if command.search(run)]
        assert steps, f"no step in the Python job runs `pytest {root}/`"
        narrowed = [run for run in steps if NARROWING.search(run[run.index("pytest"):])]
        assert not narrowed, f"these steps leave out part of {root}/: {narrowed}"
        assert command.search(makefile), f"no Makefile target runs `pytest {root}/`"


def test_the_roots_ci_ran_before_848_leave_agents_tests_unrun() -> None:
    """The negative control on the real tree: `agents/tests/` ran in no job for 180 days.

    #848 gave it its step. Without that root the check must name its files; an
    empty file list, or a matcher that takes in too much, would fail here.
    """
    before = [root for root in PYTEST_ROOTS if root != "agents/tests"]
    stray = outside_pytest(_python_test_files(), before)
    assert any(f.startswith("agents/tests/") for f in stray)


# ─── Web: the Vitest include ─────────────────────────────────────────────────


def glob_regex(glob: str) -> re.Pattern[str]:
    """Read a Vitest `include` glob as a regex.

    `**/` is any run of directories, `*` any part of one name, and `{a,b}`
    either word. Any other glob syntax raises, so the check never guesses.
    """
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "{" and "}" in glob[i:]:
            end = glob.index("}", i)
            words = glob[i + 1:end].split(",")
            out, i = out + "(?:" + "|".join(map(re.escape, words)) + ")", end + 1
        elif glob[i] in "?[](){}!+@":
            raise ValueError(f"cannot read the glob {glob!r}")
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out)


def outside_vitest(files: Iterable[str], includes: list[str]) -> list[str]:
    """The JS test files outside the web console or its Vitest `include`, sorted."""
    patterns = [glob_regex(glob) for glob in includes]
    return sorted(
        f for f in files
        if not _within(WEB, f)
        or not any(p.fullmatch(f[len(WEB) + 1:]) for p in patterns)
    )


def vitest_includes() -> list[str]:
    """The `include` globs in the web console's Vitest config."""
    config = _read(f"{WEB}/vite.config.js")
    assert not re.search(r"\b(?:exclude|root|dir)\s*:", config), (
        f"{WEB}/vite.config.js sets `exclude`, `root` or `dir`, which change what "
        "its `include` runs; teach this test what they mean"
    )
    m = re.search(r"\btest:\s*\{[^}]*?\binclude:\s*\[([^\]]*)\]", config)
    assert m, f"{WEB}/vite.config.js sets no `test.include`; teach this test Vitest's default"
    return re.findall(r"""["']([^"']+)["']""", m.group(1))


def test_every_web_test_file_matches_the_vitest_include() -> None:
    files = [f for f in _git_ls_files() if JS_TEST.search(f)]
    assert files, "found no web test files, so this check would pass on nothing"
    includes = vitest_includes()
    stray = outside_vitest(files, includes)
    assert not stray, (
        f"no CI step runs these JS test files: {stray}. Vitest runs {includes} "
        f"under {WEB}/ ({WEB}/vite.config.js)."
    )


def test_the_web_job_runs_the_whole_vitest_include() -> None:
    assert "make ui-test" in _runs("web-console"), "the web-console job must run `make ui-test`"
    recipe = makefile_recipe_body("ui-test")
    assert "cd $(WEB_DIR)" in recipe and "$(NPM) test" in recipe, recipe
    assert re.search(rf"^WEB_DIR\s*:=\s*{WEB}\s*$", _read("Makefile"), re.M), "WEB_DIR is not web"
    script = json.loads(_read(f"{WEB}/package.json"))["scripts"].get("test")
    assert script == "vitest run", f"`npm test` must run Vitest's whole include, not {script!r}"


# ─── Rust: the CLI crate ─────────────────────────────────────────────────────


def outside_crate(files: Iterable[str]) -> list[str]:
    """The Rust test files outside the CLI crate, sorted."""
    return sorted(f for f in files if not _within(CRATE, f))


def test_every_rust_test_file_is_in_the_crate_ci_tests() -> None:
    files = [f for f in _git_ls_files("*.rs") if RUST_TEST.search(_read(f))]
    assert files, "found no Rust test files, so this check would pass on nothing"
    stray = outside_crate(files)
    assert not stray, f"no CI step runs the Rust tests in {stray}: CI runs `cargo test` in {CRATE}/"
    assert _git_ls_files("Cargo.toml", "*/Cargo.toml") == [f"{CRATE}/Cargo.toml"], (
        f"a second Cargo.toml: `cargo test` in {CRATE}/ does not build another package"
    )


def test_the_rust_job_runs_cargo_test_in_the_crate() -> None:
    assert f"cd {CRATE} && cargo test" in _runs("rust"), (
        f"the rust job must run `cd {CRATE} && cargo test`"
    )


# ─── Go: what `./...` cannot reach ───────────────────────────────────────────


def skipped_by_go(path: str) -> bool:
    """True when `go test ./...` never runs the Go test file `path`.

    Go ignores a file or directory whose name starts with `.` or `_`. A file
    under `testdata` is a fixture by Go's convention, not a test.
    """
    parts = PurePosixPath(path).parts
    return "testdata" not in parts and any(name.startswith((".", "_")) for name in parts)


def test_go_test_dot_dot_dot_reaches_every_go_test_file() -> None:
    """`make test-go` runs `go test ./...`, which test_go_test_gate_ci.py pins.

    `./...` does not reach into a nested module, and skips the names above.
    """
    assert _git_ls_files("go.mod", "*/go.mod") == ["go.mod"], (
        "a nested Go module: `go test ./...` from the repo root does not reach into it"
    )
    files = _git_ls_files("*_test.go")
    assert files, "found no Go test files, so this check would pass on nothing"
    hidden = sorted(f for f in files if skipped_by_go(f))
    assert not hidden, f"`go test ./...` never runs {hidden}: a name starts with `.` or `_`"


# ─── the matchers, on made-up paths ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("root", "path", "expected"),
    [
        ("tests/integration", "tests/integration/test_a.py", True),
        ("tests/integration", "tests/integration/deep/er/test_a.py", True),
        ("tests/integration", "tests/integration_old/test_a.py", False),  # a shared name prefix
        ("tests/unit/python", "evaluators/tests/test_a.py", False),
        ("agents/tests", "agents/tests/.cache/test_a.py", False),  # pytest's norecursedirs
        ("agents/tests", "agents/tests/build/test_a.py", False),
    ],
)
def test_collected_by_pytest(root: str, path: str, expected: bool) -> None:
    assert collected_by_pytest(root, path) is expected


def test_a_python_test_file_outside_every_root_is_named() -> None:
    files = ["tests/unit/python/test_a.py", "agents/tests/test_b.py", "evaluators/tests/test_c.py"]
    assert outside_pytest(files, PYTEST_ROOTS) == ["evaluators/tests/test_c.py"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("web/src/a.test.js", True),
        ("web/src/lib/x/b.spec.js", True),
        ("web/tests/a.test.js", False),  # outside the include
        ("web/src/a.test.ts", False),
        ("webx/src/a.test.js", False),  # outside the web console
    ],
)
def test_the_vitest_include_takes_in(path: str, expected: bool) -> None:
    assert (outside_vitest([path], ["src/**/*.{test,spec}.js"]) == []) is expected


def test_an_unreadable_vitest_glob_fails_loudly() -> None:
    with pytest.raises(ValueError, match="cannot read the glob"):
        glob_regex("src/**/*.[jt]s")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("#[cfg(test)]\nmod tests {}", True),
        ('#[tokio::test(flavor = "multi_thread")]\nasync fn t() {}', True),
        ("#[derive(Debug)]\nstruct Testing;", False),
    ],
)
def test_a_rust_test_file_is_told_by_its_test_attributes(source: str, expected: bool) -> None:
    assert bool(RUST_TEST.search(source)) is expected


def test_a_rust_test_file_outside_the_crate_is_named() -> None:
    files = ["cli/src/main.rs", "clix/src/a.rs", "tools/b.rs"]
    assert outside_crate(files) == ["clix/src/a.rs", "tools/b.rs"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("internal/x/a_test.go", False),
        ("internal/_old/a_test.go", True),
        ("internal/.cache/a_test.go", True),
        ("internal/x/_a_test.go", True),
        ("internal/x/testdata/a_test.go", False),  # a fixture, not a test
    ],
)
def test_skipped_by_go(path: str, expected: bool) -> None:
    assert skipped_by_go(path) is expected
