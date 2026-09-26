"""Pin the license tools the Makefile installs.

`make check-licenses-go`, which CI's required license job runs, and `make
notices` installed `go-licenses@latest` and an unpinned `cargo-license`. A new
release of either could change what the check accepts or what the notices file
says without any change in this repository. Both now install at a version
pinned in the Makefile, the targets that run them refuse any other version,
and CI installs go-licenses with the same make target a developer runs.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from _test_infra import ci_job_steps, makefile_recipe_body

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
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


def _makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _prerequisites(target: str) -> list[str]:
    rule = re.search(rf"^{re.escape(target)}:([^\n#]*)", _makefile(), re.M)
    assert rule, f"no `{target}` rule in the Makefile"
    return rule.group(1).split()


def _cargo_license_pin() -> str:
    pins = CARGO_LICENSE_PIN_RE.findall(_makefile())
    assert pins, "no CARGO_LICENSE_VERSION := X.Y.Z pin in the Makefile"
    return str(pins[0])


def _executable(path: Path, script: str) -> Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make(*args: str, path_prefix: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["make", "--no-print-directory", "-s", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _fake_cargo(tmp_path: Path, installed: str) -> Path:
    (tmp_path / "installed").write_text(installed, encoding="utf-8")
    return _executable(tmp_path / "cargo", FAKE_CARGO)


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
