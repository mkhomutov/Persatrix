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

A new pytest tree is added to PYTEST_ROOTS, and a new web tree to the Vitest
`include`, in the same PR as its CI step, so a tree left without a runner is a
visible, reviewed diff. Files and configs are read as text; nothing here calls
pytest, Vitest, cargo or Go.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any

from _test_infra import REPO_ROOT, ci_workflow, makefile_recipe_body

#: Each tree the Python job runs pytest on, with the make target that runs it too.
PYTEST_ROOTS = {
    "tests/unit/python": "test-python",
    "agents/tests": "test-agents",
    "tests/integration": "test-integration",
}
#: The web console: its Vitest config, whose `include` globs are relative to it.
WEB = "web"
#: The CLI crate: `cargo test` run in it runs the tests of the targets Cargo finds.
CRATE = "cli"

# pytest's default `python_files`; test_pytest_collects_by_its_defaults pins that
# nothing in the repository overrides it.
PYTHON_TEST = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
# pytest's default `norecursedirs`: it collects nothing below these.
PYTEST_SKIPS = re.compile(r"\..*|.*\.egg|_darcs|build|CVS|dist|node_modules|venv|\{arch\}")
# Options that make pytest leave out part of a tree it is given, attached values too.
NARROWING = re.compile(
    r"(?:^|\s)(?:-[kmo]|@"
    r"|--(?:ignore|ignore-glob|deselect|co|collect-only|override-ini)(?=[\s=]|$))"
)
# What else shrinks a pytest run: ini options, a conftest, a config found on the way up.
PYTEST_INI_NARROWING = ("addopts", "python_files", "norecursedirs")
COLLECT_IGNORE = re.compile(r"\b(?:collect_ignore(?:_glob)?|pytest_ignore_collect)\b")
PYTEST_CONFIGS = (
    "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "pyproject.toml", "tox.ini",
    "setup.cfg",
)
# Vitest's default test-file names, `**/*.{test,spec}.?(c|m)[jt]s?(x)`.
JS_TEST = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
# The files Vitest takes the web console's config from; it uses the first it finds.
VITEST_CONFIG = re.compile(rf"{WEB}/vite(?:st)?\.(?:config|workspace)\.[cm]?[jt]s")
# A JavaScript comment, or a string literal.
JS_TOKEN = re.compile(
    r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`""", re.S
)
# A Rust file that holds tests: a test module, or a test attribute of std or a test crate.
RUST_TEST = re.compile(r"#!?\[\s*(?:cfg\s*\([^\]]*\btest\b|[\w:]*test|quickcheck)")
# Go's known GOOS and GOARCH: `x_<os>_test.go` or `x_<arch>_test.go` builds only there.
GO_OS = frozenset(
    "aix android darwin dragonfly freebsd hurd illumos ios js linux nacl netbsd openbsd"
    " plan9 solaris wasip1 windows zos".split()
)
GO_ARCH = frozenset(
    "386 amd64 amd64p32 arm armbe arm64 arm64be loong64 mips mipsle mips64 mips64le"
    " mips64p32 mips64p32le ppc ppc64 ppc64le riscv riscv64 s390 s390x sparc sparc64 wasm".split()
)


def _git_ls_files(*pathspecs: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    # A file with an unresolved conflict is listed once per stage: count it once.
    return list(dict.fromkeys(name for name in out.split("\0") if name))


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _recipe(target: str, makefile: str) -> str:
    """`target`'s recipe in `makefile`, or "" when it has none."""
    try:
        return makefile_recipe_body(target, makefile)
    except AssertionError:
        return ""


def gating_runs(workflow: dict[str, Any], job: str) -> list[str]:
    """The `run` of each step of `job` that runs on every PR and fails the build if it fails.

    An `if:` on the job or the step can skip pull requests and `continue-on-error`
    lets a failure pass, so neither step counts; nor does one that an `env:` at any
    level gives PYTEST_ADDOPTS, which narrows every pytest run it reaches.
    """
    spec = workflow["jobs"][job]
    if "if" in spec or spec.get("continue-on-error"):
        return []
    env = {**(workflow.get("env") or {}), **(spec.get("env") or {})}
    return [
        str(step["run"]).strip()
        for step in spec["steps"]
        if "run" in step and "if" not in step and not step.get("continue-on-error")
        and "PYTEST_ADDOPTS" not in {**env, **(step.get("env") or {})}
    ]


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


def pytest_runs_whole(text: str, root: str) -> bool:
    """True when a shell command in `text` runs pytest on all of `root`.

    The command starts with pytest, after any variable assignments and `python
    -m`, so a comment or an `echo` naming it does not count. Nothing up to the
    next `;`, `&`, `|` or line end may narrow it, and no PYTEST_ADDOPTS reach it.
    """
    command = re.compile(
        r"(?:^|[;&|])\s*[@+]?(?:\w+=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*(?:(?:python3?|\$\(PYTHON\))"
        rf"\s+-m\s+)?pytest {re.escape(root)}/?(?=\s|$)([^\n;&|]*)",
        re.M,
    )
    return "PYTEST_ADDOPTS" not in text and any(
        not NARROWING.search(m.group(1)) for m in command.finditer(text)
    )


def pytest_root_problems(runs: list[str], makefile: str) -> list[str]:
    """Where a pytest root does not run whole: the Python job, its make target, `make test`.

    A CI step counts when it runs pytest on the root, or runs the root's make target.
    """
    test = re.search(r"^test:([^#\n]*)", makefile, re.M)
    problems = []
    for root, target in PYTEST_ROOTS.items():
        if not pytest_runs_whole(_recipe(target, makefile), root):
            problems.append(f"`make {target}` does not run all of `pytest {root}/`")
        if test is None or target not in test.group(1).split():
            problems.append(f"`make test` does not run `{target}`")
        make = re.compile(rf"make {re.escape(target)}(?: PYTHON=\S+)?")
        if not any(pytest_runs_whole(run, root) or make.fullmatch(run) for run in runs):
            problems.append(f"no Python-job step runs all of `pytest {root}/` on every PR")
    return problems


def pytest_config_problems(
    ini: dict[str, Any], conftests: dict[str, str], tracked: Iterable[str]
) -> list[str]:
    """What, besides the command line, makes pytest collect less than a root holds.

    Two CI steps run with `-c agents/pyproject.toml`, so its ini options apply. The
    unit step passes no `-c`, so pytest takes the first config file it finds from
    the root up. A conftest can drop files from collection too.
    """
    problems = [f"agents/pyproject.toml sets `{key}`" for key in PYTEST_INI_NARROWING if key in ini]
    problems += [f"{path} drops files" for path, text in conftests.items()
                 if COLLECT_IGNORE.search(text)]
    found = set(tracked)
    for root in PYTEST_ROOTS:
        parts = PurePosixPath(root).parts
        for depth in range(len(parts) + 1):
            for name in PYTEST_CONFIGS:
                path = str(PurePosixPath(*parts[:depth], name))
                if path in found and path != "agents/pyproject.toml":
                    problems.append(f"pytest reads {path} for `pytest {root}/`")
    return problems


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
    problems = pytest_root_problems(gating_runs(ci_workflow(), "python"), _read("Makefile"))
    assert not problems, problems


def test_pytest_collects_by_its_defaults() -> None:
    """Nothing but the command line changes which files pytest collects from a root."""
    ini = tomllib.loads(_read("agents/pyproject.toml"))["tool"]["pytest"]["ini_options"]
    conftests = {path: _read(path) for path in _git_ls_files("conftest.py", "*/conftest.py")}
    problems = pytest_config_problems(ini, conftests, _git_ls_files())
    assert not problems, f"{problems} change which files pytest collects; teach this test how"


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

    `**` as a whole path segment is any run of directories, `*` any part of one
    name, and `{a,b}` any of two or more plain words. Any other glob syntax
    raises, so the check never guesses.
    """
    if glob.startswith(("/", "./", "../")):
        raise ValueError(f"cannot read the glob {glob!r}")
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i) and glob[i - 1:i] in ("", "/"):
            out, i = out + "(?:.*/)?", i + 3
        elif glob[i] == "*" and glob[i + 1:i + 2] != "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "{" and "}" in glob[i:]:
            end = glob.index("}", i)
            words = glob[i + 1:end].split(",")
            if len(words) < 2 or not all(re.fullmatch(r"[\w.-]+", word) for word in words):
                raise ValueError(f"cannot read the glob {glob!r}")
            out, i = out + "(?:" + "|".join(map(re.escape, words)) + ")", end + 1
        elif glob[i] in "*?[](){}!+@\\":
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


def _mask_js(code: str) -> str:
    """`code` with its comments blanked and the letters of its strings hidden, in place.

    A string followed by `:` is an object key and stays readable.
    """

    def mask(m: re.Match[str]) -> str:
        token = m.group()
        if token[0] == "/":
            return re.sub(r"\S", " ", token)
        if re.match(r"\s*:", code[m.end():]):
            return token
        return token[0] + "_" * (len(token) - 2) + token[-1]

    return JS_TOKEN.sub(mask, code)


def vitest_includes(config: str) -> list[str]:
    """The `test.include` globs of a Vitest config, read as JavaScript text.

    Comments do not count, and only a `test` object with nothing nested in it is
    read, so no `coverage.include` stands in for it. A key that changes what the
    include runs, a spread, or an include that is not a list of strings raises
    rather than being guessed at.
    """
    masked = _mask_js(config)
    blocks = list(re.finditer(r"""(?<![\w$.])(["']?)test\1\s*:\s*\{([^{}]*)\}""", masked))
    if len(blocks) != 1:
        raise ValueError("cannot read a single `test` object with nothing nested in it")
    body, start = blocks[0].group(2), blocks[0].start(2)
    changes = re.findall(r"(?<![\w$.])(?:exclude|dir|root|projects|workspace)\b|\.\.\.", body)
    if changes or re.search(r"""(?<![\w$.])(["']?)root\1\s*:""", masked):
        raise ValueError(f"cannot read a config that sets {changes or ['root']}")
    include = re.search(r"(?<![\w$.])include\s*:\s*\[([^\]]*)\]", body)
    if include is None:
        raise ValueError("cannot read a config with no `test.include`; teach this test the default")
    offset = start + include.start(1)
    if not re.fullmatch(r"""[\s,]*(?:(?:"_*"|'_*')[\s,]*)*""", include.group(1)):
        text = config[offset:offset + len(include.group(1))]
        raise ValueError(f"cannot read `test.include` as a list of strings: {text!r}")
    return [
        config[offset + s.start() + 1:offset + s.end() - 1]
        for s in re.finditer(r"\"_*\"|'_*'", include.group(1))
    ]


#: The one recipe `make ui-test` may have: all of Vitest's include, in web/.
UI_TEST = "\tcd $(WEB_DIR) && $(NPM) ci && $(NPM) test\n"


def ui_test_problems(runs: list[str], makefile: str, script: str | None) -> list[str]:
    """Each way the web job's `make ui-test` can run less than Vitest's whole include.

    The recipe must be exactly UI_TEST and set WEB_DIR and NPM once, for every
    target, so no argument after `npm test` and no second value slips in.
    """
    problems = []
    if "make ui-test" not in runs:
        problems.append("no web-console step runs `make ui-test` on every PR")
    if (recipe := _recipe("ui-test", makefile)) != UI_TEST:
        problems.append(f"`make ui-test` runs {recipe!r}, not {UI_TEST!r}")
    for name, value in (("WEB_DIR", WEB), ("NPM", "npm")):
        assignment = rf"^(\S*:)?[ \t]*(?:override |export )*{name}[ \t]*([:?+!]?=)[ \t]*(\S*)"
        if (sets := re.findall(assignment, makefile, re.M)) != [("", ":=", value)]:
            problems.append(f"{name} is not set once, as `{name} := {value}`: {sets}")
    if script != "vitest run":
        problems.append(f"`npm test` runs {script!r}, not `vitest run`")
    return problems


def test_every_web_test_file_matches_the_vitest_include() -> None:
    files = [f for f in _git_ls_files() if JS_TEST.search(f)]
    assert files, "found no web test files, so this check would pass on nothing"
    configs = [f for f in _git_ls_files(f"{WEB}/*") if VITEST_CONFIG.fullmatch(f)]
    assert configs == [f"{WEB}/vite.config.js"], f"Vitest uses the first config it finds: {configs}"
    includes = vitest_includes(_read(f"{WEB}/vite.config.js"))
    stray = outside_vitest(files, includes)
    assert not stray, (
        f"no CI step runs these JS test files: {stray}. Vitest runs {includes} "
        f"under {WEB}/ ({WEB}/vite.config.js)."
    )


def test_the_web_job_runs_the_whole_vitest_include() -> None:
    script = json.loads(_read(f"{WEB}/package.json"))["scripts"].get("test")
    runs = gating_runs(ci_workflow(), "web-console")
    problems = ui_test_problems(runs, _read("Makefile"), script)
    assert not problems, problems


# ─── Rust: the CLI crate ─────────────────────────────────────────────────────


def outside_crate(files: Iterable[str], manifests: Iterable[str] = ()) -> list[str]:
    """The Rust test files `cargo test` in the CLI crate never runs, sorted.

    It runs the crate's own targets: not a file outside it, in a package nested in
    it (`manifests` lists every Cargo.toml), or in its examples, benches or build script.
    """
    nested = [str(PurePosixPath(m).parent) for m in manifests if _within(CRATE, m)]
    skipped = [d for d in nested if d != CRATE] + [f"{CRATE}/examples", f"{CRATE}/benches"]
    return sorted(
        f for f in files
        if not _within(CRATE, f) or f == f"{CRATE}/build.rs"
        or any(_within(d, f) for d in skipped)
    )


def cargo_test_problems(runs: list[str], manifest: dict[str, Any]) -> list[str]:
    """Each way the rust job's `cargo test` can skip a test target of the CLI crate.

    `cargo test` runs the targets Cargo finds by its own rules. A target table or a
    discovery switch in Cargo.toml can turn tests off (`test = false`, `autotests =
    false`, `required-features`), so any of them fails until this test is taught it.
    """
    problems = []
    if f"cd {CRATE} && cargo test" not in runs:
        problems.append(f"no rust-job step runs `cd {CRATE} && cargo test` on every PR")
    targets = {"lib", "bin", "test", "example", "bench"}
    auto = {"autolib", "autobins", "autotests", "autoexamples", "autobenches", "build"}
    found = sorted(targets & manifest.keys() | auto & manifest.get("package", {}).keys())
    if found:
        problems.append(
            f"{CRATE}/Cargo.toml sets {found}, which can stop `cargo test` running a target"
        )
    return problems


def test_every_rust_test_file_is_in_the_crate_ci_tests() -> None:
    files = [f for f in _git_ls_files("*.rs") if RUST_TEST.search(_read(f))]
    assert files, "found no Rust test files, so this check would pass on nothing"
    stray = outside_crate(files, _git_ls_files("Cargo.toml", "*/Cargo.toml"))
    assert not stray, f"no CI step runs the Rust tests in {stray}: CI runs `cargo test` in {CRATE}/"


def test_the_rust_job_runs_cargo_test_in_the_crate() -> None:
    manifest = tomllib.loads(_read(f"{CRATE}/Cargo.toml"))
    problems = cargo_test_problems(gating_runs(ci_workflow(), "rust"), manifest)
    assert not problems, problems


# ─── Go: what `./...` cannot reach ───────────────────────────────────────────


def skipped_by_go(path: str, source: str = "") -> bool:
    """True when CI's `go test ./...`, on linux/amd64 with no tags, never builds `path`.

    Go ignores a file or directory whose name starts with `.` or `_`, `./...`
    skips `vendor` directories, and a `_<os>` or `_<arch>` name suffix builds a
    file only there. A `//go:build` line counts as skipped: this test cannot
    evaluate it, so teach it the expression when CI does build the file. A file
    under `testdata` is a fixture by Go's convention, not a test.
    """
    parts = PurePosixPath(path).parts
    if "testdata" in parts:
        return False
    if any(name.startswith((".", "_")) or name == "vendor" for name in parts):
        return True
    words = parts[-1].removesuffix("_test.go").split("_")[1:]  # Go skips the first word
    if len(words) > 1 and words[-2] in GO_OS and words[-1] in GO_ARCH:
        return words[-2:] != ["linux", "amd64"]
    if words and words[-1] in GO_OS | GO_ARCH:
        return words[-1] not in ("linux", "amd64")
    return re.search(r"^//go:build\b", source, re.M) is not None


def unreached_by_go(
    files: Iterable[str], manifests: Iterable[str], read: Callable[[str], str]
) -> list[str]:
    """The Go test files `go test ./...` from the repo root never runs, sorted.

    `./...` does not reach into a module nested in the repository, and skips what
    skipped_by_go names; `testdata` holds fixtures either way.
    """
    nested = tuple(m.removesuffix("go.mod") for m in manifests if m != "go.mod")
    return sorted(
        f for f in files
        if "testdata" not in PurePosixPath(f).parts
        and (f.startswith(nested) or skipped_by_go(f, read(f)))
    )


def test_go_test_dot_dot_dot_reaches_every_go_test_file() -> None:
    """`make test-go` runs `go test ./...`, which test_go_test_gate_ci.py pins."""
    files = _git_ls_files("*_test.go")
    assert files, "found no Go test files, so this check would pass on nothing"
    hidden = unreached_by_go(files, _git_ls_files("go.mod", "*/go.mod"), _read)
    assert not hidden, f"`go test ./...` never runs {hidden}: see skipped_by_go, unreached_by_go"
