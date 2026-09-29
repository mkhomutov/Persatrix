"""The runner check's matchers and pins, on made-up inputs (ISSUE-0178).

test_every_test_file_has_a_runner.py reads the real tree, where every check
passes, so a check that stopped biting would stay green there. These cases
give each matcher and pin an input it must accept and one it must name.
"""

from __future__ import annotations

import pytest

from .test_every_test_file_has_a_runner import (
    CRATE,
    PYTEST_ROOTS,
    PYTHON_TEST,
    RUST_TEST,
    UI_TEST,
    WEB,
    cargo_test_problems,
    collected_by_pytest,
    gating_runs,
    glob_regex,
    outside_crate,
    outside_pytest,
    outside_vitest,
    pytest_config_problems,
    pytest_root_problems,
    pytest_runs_whole,
    skipped_by_go,
    ui_test_problems,
    unreached_by_go,
    vitest_includes,
)


@pytest.mark.parametrize(
    ("root", "path", "expected"),
    [
        ("tests/integration", "tests/integration/test_a.py", True),
        ("tests/integration", "tests/integration/deep/er/test_a.py", True),
        ("tests/integration", "tests/integration_old/test_a.py", False),  # a shared name prefix
        ("tests/unit/python", "evaluators/tests/test_a.py", False),
    ],
)
def test_collected_by_pytest(root: str, path: str, expected: bool) -> None:
    assert collected_by_pytest(root, path) is expected


@pytest.mark.parametrize(
    "name", [".cache", "x.egg", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}"]
)
def test_pytest_collects_nothing_below_its_norecursedirs(name: str) -> None:
    assert not collected_by_pytest("agents/tests", f"agents/tests/{name}/test_a.py")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("t/test_a.py", True),
        ("t/a_test.py", True),
        ("t/conftest.py", False),
        ("t/_infra.py", False),
    ],
)
def test_a_python_test_file_is_told_by_its_name(path: str, expected: bool) -> None:
    assert bool(PYTHON_TEST.search(path)) is expected


def test_a_python_test_file_outside_every_root_is_named() -> None:
    files = ["tests/unit/python/test_a.py", "agents/tests/test_b.py", "evaluators/tests/test_c.py"]
    assert outside_pytest(files, PYTEST_ROOTS) == ["evaluators/tests/test_c.py"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m pytest agents/tests/ -v --tb=short -c agents/pyproject.toml", True),
        ('PYTHONPATH="x" python -m pytest agents/tests/ -v', True),
        ("\t$(PYTHON) -m pytest agents/tests/ -v\n", True),
        ("pip install pytest-timeout && python -m pytest agents/tests/ -v", True),
        ("python -m pytest agents/tests/ -v\npython -m coverage xml", True),
        ("python -m pytest agents/tests/ -k persona", False),
        ("python -m pytest agents/tests/ -ksmoke", False),
        ("python -m pytest agents/tests/ -m 'not slow'", False),
        ("python -m pytest agents/tests/ --ignore=agents/tests/x", False),
        ("python -m pytest agents/tests/ --deselect agents/tests/a.py::t", False),
        ("python -m pytest agents/tests/ --co", False),
        ("python -m pytest agents/tests/ -o python_files=a.py", False),
        ("python -m pytest agents/tests/ @args.txt", False),
        ('PYTEST_ADDOPTS="-k x" python -m pytest agents/tests/', False),
        ("# python -m pytest agents/tests/", False),
        ('echo "pytest agents/tests/ is off"', False),
        ("python -m pytest agents/tests/test_a.py", False),
    ],
)
def test_a_pytest_command_runs_its_whole_root(command: str, expected: bool) -> None:
    assert pytest_runs_whole(command, "agents/tests") is expected


def test_a_step_that_can_skip_a_pr_or_let_a_failure_pass_does_not_count() -> None:
    steps = [
        {"run": "a"},
        {"run": "b", "if": "github.event_name == 'push'"},
        {"run": "c", "continue-on-error": True},
        {"run": "d", "env": {"PYTEST_ADDOPTS": "-k smoke"}},
        {"uses": "actions/checkout@v7"},
    ]
    assert gating_runs({"jobs": {"j": {"steps": steps}}}, "j") == ["a"]
    assert gating_runs({"jobs": {"j": {"if": "false", "steps": steps}}}, "j") == []
    narrowed_everywhere = {"env": {"PYTEST_ADDOPTS": "-x"}, "jobs": {"j": {"steps": steps}}}
    assert gating_runs(narrowed_everywhere, "j") == []


MAKEFILE = "test: test-go " + " ".join(PYTEST_ROOTS.values()) + "\n" + "".join(
    f"{target}:\n\t$(PYTHON) -m pytest {root}/ -v\n" for root, target in PYTEST_ROOTS.items()
)
RUNS = [f"python -m pytest {root}/ -v" for root in PYTEST_ROOTS]
AGENTS_UNRUN = "no Python-job step runs all of `pytest agents/tests/` on every PR"


@pytest.mark.parametrize(
    ("runs", "makefile", "problems"),
    [
        (RUNS, MAKEFILE, []),
        ([RUNS[0], "make test-agents PYTHON=python", RUNS[2]], MAKEFILE, []),
        ([*RUNS, "python -m pytest tests/integration/ -m requires_orchestrator"], MAKEFILE, []),
        ([RUNS[0], RUNS[2]], MAKEFILE, [AGENTS_UNRUN]),
        ([RUNS[0], RUNS[1] + " -m 'not slow'", RUNS[2]], MAKEFILE, [AGENTS_UNRUN]),
        (RUNS, MAKEFILE.replace("agents/tests/ -v", "agents/tests/ -k persona"),
         ["`make test-agents` does not run all of `pytest agents/tests/`"]),
        (RUNS, MAKEFILE.replace(" test-integration\n", "\n"),
         ["`make test` does not run `test-integration`"]),
        (RUNS, MAKEFILE.replace("test-integration:\n\t", "# was: "),
         ["`make test-integration` does not run all of `pytest tests/integration/`"]),
    ],
)
def test_the_pytest_pin_names_a_root_that_does_not_run_whole(
    runs: list[str], makefile: str, problems: list[str]
) -> None:
    assert pytest_root_problems(runs, makefile) == problems


def test_a_pytest_setting_that_drops_files_is_named() -> None:
    problems = pytest_config_problems(
        {"addopts": "--ignore=agents/tests/x", "asyncio_mode": "auto"},
        {"tests/conftest.py": 'collect_ignore_glob = ["test_live_*.py"]', "a/conftest.py": ""},
        ["agents/pyproject.toml", "tests/pytest.ini", "web/pytest.ini"],
    )
    assert problems == [
        "agents/pyproject.toml sets `addopts`",
        "tests/conftest.py drops files",
        "pytest reads tests/pytest.ini for `pytest tests/unit/python/`",
        "pytest reads tests/pytest.ini for `pytest tests/integration/`",
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("web/src/a.test.js", True),
        ("web/src/lib/x/b.spec.js", True),
        ("web/tests/a.test.js", False),  # outside the include
        ("web/src/a.test.ts", False),
        ("webx/src/a.test.js", False),  # outside the web console
        ("app/src/a.test.js", False),  # outside it, with a name as long as `web`
    ],
)
def test_the_vitest_include_takes_in(path: str, expected: bool) -> None:
    assert (outside_vitest([path], ["src/**/*.{test,spec}.js"]) == []) is expected


def test_a_star_in_a_vitest_glob_stays_in_one_directory() -> None:
    files = ["web/src/a.test.js", "web/src/lib/b.test.js"]
    assert outside_vitest(files, ["src/*.test.js"]) == ["web/src/lib/b.test.js"]


@pytest.mark.parametrize(
    "glob",
    [
        "src/**/*.[jt]s",
        "src**/*.test.js",  # `**` inside a name is `*` to Vitest
        "src/**",
        "src/**/*.{test}.js",  # a one-word brace is literal to Vitest
        "src/**/{*.test,*.spec}.js",
        "./src/**/*.test.js",
        "!src/legacy/**",
    ],
)
def test_an_unreadable_vitest_glob_fails_loudly(glob: str) -> None:
    with pytest.raises(ValueError, match="cannot read the glob"):
        glob_regex(glob)


VITE = 'export default {\n  optimizeDeps: { exclude: ["x"] },\n  // test: { include: ["b"] },\n'


@pytest.mark.parametrize(
    ("test_block", "expected"),
    [
        ('environment: "jsdom", include: ["src/**/*.{test,spec}.js"]', ["src/**/*.{test,spec}.js"]),
        ('include: [\n  // "src/**/*.js",\n  "src/lib/*.test.js",\n]', ["src/lib/*.test.js"]),
    ],
)
def test_the_vitest_reader_takes_the_test_include(test_block: str, expected: list[str]) -> None:
    assert vitest_includes(VITE + f"  test: {{ {test_block} }},\n}};") == expected


@pytest.mark.parametrize(
    "test_block",
    [
        'coverage: { include: ["src/**"] }, include: ["src/lib/*.test.js"]',
        'include: ["src/**/*.test.js"], exclude: ["src/panels/**"]',
        'include: ["src/**/*.test.js"], "exclude": ["src/panels/**"]',
        'exclude, include: ["src/**/*.test.js"]',
        '...configDefaults, include: ["src/**/*.test.js"]',
        'include: [process.env.CI ? "src/lib/*.test.js" : "src/**/*.test.js"]',
        'root: "src", include: ["**/*.test.js"]',
        'environment: "jsdom"',
    ],
)
def test_a_vitest_config_this_test_cannot_read_fails(test_block: str) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        vitest_includes(VITE + f"  test: {{ {test_block} }},\n}};")


UI_MAKEFILE = f"NPM           := npm\nWEB_DIR       := {WEB}\nui-test:\n{UI_TEST}"


@pytest.mark.parametrize(
    ("makefile", "script", "problem"),
    [
        (UI_MAKEFILE, "vitest run", None),
        (UI_MAKEFILE.replace("test\n", "test -- src/lib\n"), "vitest run", "`make ui-test` runs"),
        (UI_MAKEFILE + "WEB_DIR := console\n", "vitest run", "WEB_DIR is not set once"),
        (UI_MAKEFILE + "ui-test: WEB_DIR := web/legacy\n", "vitest run", "WEB_DIR is not set once"),
        (UI_MAKEFILE.replace(":= npm", ":= true npm"), "vitest run", "NPM is not set once"),
        (UI_MAKEFILE, "vitest run src/lib", "`npm test` runs"),
    ],
)
def test_the_web_pin_names_a_ui_test_that_runs_less(
    makefile: str, script: str, problem: str | None
) -> None:
    problems = ui_test_problems(["make ui-test"], makefile, script)
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problems[0].startswith(problem), problems


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("#[cfg(test)]\nmod tests {}", True),
        ("#[test]\nfn t() {}", True),
        ('#[tokio::test(flavor = "multi_thread")]\nasync fn t() {}', True),
        ("#[rstest]\nfn t() {}", True),
        ("#[cfg(all(test, unix))]\nmod tests {}", True),
        ("#[derive(Debug)]\nstruct Testing;", False),
    ],
)
def test_a_rust_test_file_is_told_by_its_test_attributes(source: str, expected: bool) -> None:
    assert bool(RUST_TEST.search(source)) is expected


def test_a_rust_test_file_outside_the_crate_is_named() -> None:
    files = ["cli/src/main.rs", "clix/src/a.rs", "tools/b.rs"]
    assert outside_crate(files) == ["clix/src/a.rs", "tools/b.rs"]
    unrun = ["cli/benches/b.rs", "cli/build.rs", "cli/examples/e.rs", "cli/fuzz/f.rs"]
    manifests = ["cli/Cargo.toml", "cli/fuzz/Cargo.toml", "Cargo.toml"]
    assert outside_crate([*unrun, "cli/tests/it.rs"], manifests) == unrun


def test_a_cargo_setting_that_can_turn_tests_off_is_named() -> None:
    runs = [f"cd {CRATE} && cargo test"]
    assert cargo_test_problems(runs, {"package": {"name": "persatrix"}}) == []
    tests_off = {"package": {"autotests": False}, "bin": [{"test": False}]}
    assert cargo_test_problems(runs, tests_off) == [
        f"{CRATE}/Cargo.toml sets ['autotests', 'bin'],"
        " which can stop `cargo test` running a target"
    ]
    assert cargo_test_problems([f"cd {CRATE} && cargo test --lib"], {}) == [
        f"no rust-job step runs `cd {CRATE} && cargo test` on every PR"
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("internal/x/a_test.go", False),
        ("internal/_old/a_test.go", True),
        ("internal/.cache/a_test.go", True),
        ("internal/x/_a_test.go", True),
        ("internal/x/testdata/a_test.go", False),  # a fixture, not a test
        ("internal/x/vendor/v/a_test.go", True),
        ("internal/x/a_windows_test.go", True),
        ("internal/x/a_arm64_test.go", True),
        ("internal/x/a_windows_amd64_test.go", True),
        ("internal/x/a_linux_test.go", False),
        ("internal/x/a_linux_amd64_test.go", False),
        ("internal/x/windows_test.go", False),  # Go reads no suffix from the first word
    ],
)
def test_skipped_by_go(path: str, expected: bool) -> None:
    assert skipped_by_go(path) is expected


def test_a_go_build_line_or_a_nested_module_hides_a_test_file() -> None:
    assert skipped_by_go("internal/x/e2e_test.go", "//go:build integration\n\npackage x\n")
    files = ["internal/x/a_test.go", "tools/b_test.go", "internal/c/testdata/m/c_test.go"]
    manifests = ["go.mod", "tools/go.mod", "internal/c/testdata/m/go.mod"]
    assert unreached_by_go(files, manifests, lambda path: "package x\n") == ["tools/b_test.go"]
