"""Pin the license tools the Makefile installs.

`make check-licenses-go`, which CI's required license job runs, and `make
notices` installed `go-licenses@latest` and an unpinned `cargo-license`. A new
release of either could change what the check accepts or what the notices file
says without any change in this repository. Both now install at a version
pinned in the Makefile, the targets that run them refuse any other version,
and CI installs go-licenses with the same make target a developer runs. Each
target then runs the binary whose version it checked.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from _test_infra import REPO_ROOT, ci_job_steps, makefile_recipe_body

from scripts import generate_third_party_notices as notices

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GO_LICENSES_MODULE = "github.com/google/go-licenses"
GO_LICENSES_PIN_RE = re.compile(r"^GO_LICENSES_VERSION\s*:=\s*(v\d+\.\d+\.\d+)\s*$", re.M)
CARGO_LICENSE_PIN_RE = re.compile(r"^CARGO_LICENSE_VERSION\s*:=\s*(\d+\.\d+\.\d+)\s*$", re.M)
GO_INSTALL_RE = re.compile(r"\bgo install\s+(\S+)")
CARGO_INSTALL_RE = re.compile(r"(?:\bcargo|\$\(CARGO\))\s+install\b(?!\s+--list)")

# Stands in for `cargo`. `install --list` reports the cargo-license version
# recorded in ./installed (none when the file is empty); any other call is
# appended to ./calls, and an install records the version it was asked for.
FAKE_CARGO = """#!/bin/sh
dir=$(dirname "$0")
if [ "$1" = install ] && [ "$2" = --list ]; then
    if [ -s "$dir/installed" ]; then
        printf 'cargo-license v%s:\\n    cargo-license\\n' "$(cat "$dir/installed")"
    fi
    exit 0
fi
echo "$*" >> "$dir/calls"
while [ $# -gt 0 ]; do
    if [ "$1" = --version ]; then printf '%s' "$2" > "$dir/installed"; fi
    shift
done
"""

# Stands in for `go`. `version -m` prints the build information Go records in
# a binary, for the "module version" in ./built (none when the file is empty);
# `install` is appended to ./calls, records what it built in ./built and
# writes a go-licenses into $GOBIN; `env NAME` prints that variable.
FAKE_GO = """#!/bin/sh
dir=$(dirname "$0")
case "$1" in
version)
    if [ ! -s "$dir/built" ]; then
        echo "$3: could not read Go build info" >&2
        exit 1
    fi
    read -r module version < "$dir/built"
    printf '%s: go1.26.3\\n\\tpath\\t%s\\n' "$3" "$module"
    printf '\\tmod\\t%s\\t%s\\th1:fake=\\n' "$module" "$version"
    printf '\\tdep\\tgithub.com/emirpasic/gods\\tv1.12.0\\th1:fake=\\n'
    ;;
install)
    echo "$*" >> "$dir/calls"
    echo "${2%@*} ${2##*@}" > "$dir/built"
    printf '#!/bin/sh\\n' > "${GOBIN:-$dir}/go-licenses"
    chmod +x "${GOBIN:-$dir}/go-licenses"
    ;;
env)
    printenv "$2"
    ;;
esac
"""

# Stands in for go-licenses: appends its arguments to ./calls beside it.
FAKE_GO_LICENSES = """#!/bin/sh
echo "$*" >> "$(dirname "$0")/calls"
"""

# Stands in for Python in the notices targets: records which binaries the
# notices script was told to run.
FAKE_PYTHON = """#!/bin/sh
printf '%s\\n%s\\n' "$GO_LICENSES" "$CARGO_LICENSE" > "$(dirname "$0")/told"
"""


def _makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _prerequisites(target: str) -> list[str]:
    rule = re.search(rf"^{re.escape(target)}:([^\n#]*)", _makefile(), re.M)
    assert rule, f"no `{target}` rule in the Makefile"
    return rule.group(1).split()


def _go_licenses_pin() -> str:
    pins = GO_LICENSES_PIN_RE.findall(_makefile())
    assert pins, "no GO_LICENSES_VERSION := vX.Y.Z pin in the Makefile"
    return str(pins[0])


def _cargo_license_pin() -> str:
    pins = CARGO_LICENSE_PIN_RE.findall(_makefile())
    assert pins, "no CARGO_LICENSE_VERSION := X.Y.Z pin in the Makefile"
    return str(pins[0])


def _executable(path: Path, script: str) -> Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make(
    *args: str, path_prefix: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = {**os.environ, **(env or {})}
    if path_prefix is not None:
        run_env["PATH"] = f"{path_prefix}{os.pathsep}{run_env.get('PATH', '')}"
    return subprocess.run(
        ["make", "--no-print-directory", "-s", *args],
        cwd=REPO_ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _fake_cargo(tmp_path: Path, installed: str) -> Path:
    (tmp_path / "installed").write_text(installed, encoding="utf-8")
    return _executable(tmp_path / "cargo", FAKE_CARGO)


def _fake_go(tmp_path: Path, built: str) -> Path:
    """A fake `go` in tmp_path/bin; returns that directory, for PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "built").write_text(built, encoding="utf-8")
    _executable(bin_dir / "go", FAKE_GO)
    return bin_dir


def _fake_go_licenses(tmp_path: Path) -> Path:
    """A fake go-licenses in a directory PATH does not search."""
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    return _executable(pinned / "go-licenses", FAKE_GO_LICENSES)


def test_makefile_pins_each_license_tool_once() -> None:
    makefile = _makefile()
    assert len(GO_LICENSES_PIN_RE.findall(makefile)) == 1, (
        "expected one GO_LICENSES_VERSION := vX.Y.Z pin"
    )
    assert len(CARGO_LICENSE_PIN_RE.findall(makefile)) == 1, (
        "expected one CARGO_LICENSE_VERSION := X.Y.Z pin"
    )


def test_no_tool_install_floats() -> None:
    """Every `go install` names a version, and every `cargo install` a locked
    one, in the Makefile and in every workflow."""
    for path in [REPO_ROOT / "Makefile", *sorted(WORKFLOWS.glob("*.y*ml"))]:
        for line in path.read_text(encoding="utf-8").splitlines():
            for package in GO_INSTALL_RE.findall(line):
                version = package.partition("@")[2]
                assert version and version not in {"latest", "master", "main"}, (
                    f"{path.name}: `{line.strip()}` installs whatever is newest"
                )
            if CARGO_INSTALL_RE.search(line):
                assert "--locked" in line and "--version" in line, (
                    f"{path.name}: `{line.strip()}` installs whatever is newest"
                )


def test_makefile_builds_cargo_tools_in_cli() -> None:
    """rustup picks the toolchain from the working directory. A `cargo install`
    run in cli/ builds the tool with the release cli/rust-toolchain.toml pins,
    as CI's installs do, not with a default toolchain that may be too old."""
    for line in _makefile().splitlines():
        install = CARGO_INSTALL_RE.search(line)
        if install:
            assert "cd cli && " in line[: install.start()], f"`{line.strip()}` builds outside cli/"


def test_targets_that_run_the_tools_check_their_versions_first() -> None:
    assert "go-licenses-pinned" in _prerequisites("check-licenses-go")
    for target in ("notices", "notices-check"):
        assert {"go-licenses-pinned", "cargo-license-pinned"} <= set(_prerequisites(target)), (
            f"`{target}` runs both tools, so it must check both versions"
        )
    go_check = makefile_recipe_body("go-licenses-pinned")
    assert "$(GO_LICENSES_VERSION)" in go_check and "go-licenses-install" in go_check
    cargo_check = makefile_recipe_body("cargo-license-pinned")
    assert "$(CARGO_LICENSE_VERSION)" in cargo_check and "cargo-license-install" in cargo_check


def test_ci_installs_go_licenses_with_the_make_target() -> None:
    runs = [str(step.get("run", "")).strip() for step in ci_job_steps("licenses")]
    assert runs.count("make go-licenses-install") == 1, (
        "CI must install go-licenses with `make go-licenses-install`, the Makefile's pin"
    )
    assert runs.index("make go-licenses-install") < runs.index("make check-licenses-go")


def test_go_licenses_check_refuses_a_binary_it_cannot_verify(tmp_path: Path) -> None:
    """go-licenses v1 has no version command, so the check reads the module
    and version Go records in the binary. A binary without that record (a
    local build, a script, another tool of the same name) is refused, not
    run."""
    _executable(tmp_path / "go-licenses", "#!/bin/sh\nexit 0\n")
    result = _make("go-licenses-pinned", path_prefix=tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "make go-licenses-install" in output, output


def test_go_licenses_check_runs_the_binary_it_verified(tmp_path: Path) -> None:
    """GO_LICENSES points the check at one binary, wherever PATH would look:
    a pinned build passes, and it is the one `check-licenses-go` runs."""
    go_bin = _fake_go(tmp_path, built=f"{GO_LICENSES_MODULE} {_go_licenses_pin()}")
    go_licenses = _fake_go_licenses(tmp_path)
    result = _make("check-licenses-go", f"GO_LICENSES={go_licenses}", path_prefix=go_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    ran = (go_licenses.parent / "calls").read_text(encoding="utf-8")
    assert ran.startswith("check ./cmd/... ./internal/..."), ran
    assert not (go_bin / "calls").exists(), "the pinned version needs no install"


def test_go_licenses_check_installs_the_pin_when_missing(tmp_path: Path) -> None:
    go_bin = _fake_go(tmp_path, built="")
    gobin = tmp_path / "gobin"
    gobin.mkdir()
    result = _make(
        "go-licenses-pinned",
        f"GO_LICENSES={gobin / 'go-licenses'}",
        path_prefix=go_bin,
        env={"GOBIN": str(gobin)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (go_bin / "calls").read_text(encoding="utf-8").splitlines()
    assert calls == [f"install {GO_LICENSES_MODULE}@{_go_licenses_pin()}"]


def test_go_licenses_check_says_where_an_install_off_path_went(tmp_path: Path) -> None:
    """When `go install` puts the pin in a directory PATH does not search,
    running `make go-licenses-install` again cannot help, so the check names
    that directory. A name PATH cannot find stands in for such a directory."""
    go_bin = _fake_go(tmp_path, built="")
    gobin = tmp_path / "gobin"
    gobin.mkdir()
    result = _make(
        "go-licenses-pinned",
        "GO_LICENSES=go-licenses-not-on-path",
        path_prefix=go_bin,
        env={"GOBIN": str(gobin)},
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert f"{gobin}/go-licenses" in output and "GO_LICENSES=" in output, output
    assert "make go-licenses-install" not in output, output


def test_cargo_license_check_refuses_another_version(tmp_path: Path) -> None:
    cargo = _fake_cargo(tmp_path, installed="0.6.1")
    result = _make("cargo-license-pinned", f"CARGO={cargo}")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "0.6.1" in output and "make cargo-license-install" in output, output
    assert not (tmp_path / "calls").exists(), "another version is refused, not replaced"


def test_cargo_license_check_accepts_the_pin(tmp_path: Path) -> None:
    cargo = _fake_cargo(tmp_path, installed=_cargo_license_pin())
    result = _make("cargo-license-pinned", f"CARGO={cargo}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "calls").exists(), "the pinned version needs no install"


def test_cargo_license_check_installs_the_pin_when_missing(tmp_path: Path) -> None:
    cargo = _fake_cargo(tmp_path, installed="")
    result = _make("cargo-license-pinned", f"CARGO={cargo}")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8").split()
    assert calls == ["install", "cargo-license", "--locked", "--version", _cargo_license_pin()]


@pytest.mark.parametrize("target", ["notices", "notices-check"])
def test_notices_runs_the_binaries_the_checks_verified(tmp_path: Path, target: str) -> None:
    """The version checks read one go-licenses and the cargo-license cargo
    recorded installing. `cargo license` would run whichever copy comes first
    on PATH, so the notices script is told which binaries to run."""
    go_bin = _fake_go(tmp_path, built=f"{GO_LICENSES_MODULE} {_go_licenses_pin()}")
    go_licenses = _fake_go_licenses(tmp_path)
    cargo = _fake_cargo(tmp_path, installed=_cargo_license_pin())
    python = _executable(tmp_path / "python", FAKE_PYTHON)
    cargo_home = tmp_path / "cargo-home"
    result = _make(
        target,
        f"GO_LICENSES={go_licenses}",
        f"CARGO={cargo}",
        f"PYTHON={python}",
        path_prefix=go_bin,
        env={"CARGO_HOME": str(cargo_home), "CARGO_INSTALL_ROOT": ""},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    told = (tmp_path / "told").read_text(encoding="utf-8").splitlines()
    assert told == [str(go_licenses), str(cargo_home / "bin" / "cargo-license")]


def test_notices_script_runs_the_binaries_it_is_told(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: Path | None = None) -> str:
        ran.append(cmd)
        return "[]"

    monkeypatch.setattr(notices, "_run", fake_run)
    monkeypatch.setenv("GO_LICENSES", "/pinned/go-licenses")
    monkeypatch.setenv("CARGO_LICENSE", "/pinned/cargo-license")
    notices.collect_go()
    notices.collect_rust()
    # Run by hand, outside make, the script finds the tools as it always did.
    monkeypatch.delenv("GO_LICENSES")
    monkeypatch.delenv("CARGO_LICENSE")
    notices.collect_go()
    notices.collect_rust()
    assert [cmd[:2] for cmd in ran] == [
        ["/pinned/go-licenses", "report"],
        ["/pinned/cargo-license", "license"],
        ["go-licenses", "report"],
        ["cargo", "license"],
    ]
