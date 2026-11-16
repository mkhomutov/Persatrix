"""Hold the agent image to the versions CI pins (ISSUE-0219).

``Dockerfile.agent`` ran ``pip install ./agents`` with no constraints, so an
image took the newest release of every dependency on the day it was built,
while every CI job tested the pinned ones. On 2026-11-16 an image built that
way held 22 packages at another version than the pins.

These tests keep the parts that changed that:

- the image's one install line passes the provider SDKs' constraints file,
  which holds every pin of the plain file too, so it serves an image built
  with or without ``AGENT_EXTRAS``;
- that file reaches the build (``.dockerignore`` does not leave it out);
- every extra a compose overlay builds with is covered by that file;
- ``make image-pins-check`` builds the image both ways and compares each
  with the pins, and the comparison (``scripts/checks/image_pins.py``) reads a
  pin as the Python it runs on reads it.

Nothing here builds an image: the weekly image run does.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from _test_infra import REPO_ROOT, makefile_recipe_body
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.checks import image_pins

DOCKERFILE = REPO_ROOT / "Dockerfile.agent"
PROVIDERS = ".github/python-constraints-providers.txt"


def _instructions() -> list[str]:
    """Dockerfile.agent's instructions, comments left out, continuation lines joined."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _pins() -> dict[str, list[Requirement]]:
    """Every pin of the providers file by package, whatever its marker."""
    pins: dict[str, list[Requirement]] = {}
    for raw in (REPO_ROOT / PROVIDERS).read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            requirement = Requirement(line)
            pins.setdefault(canonicalize_name(requirement.name), []).append(requirement)
    return pins


# ─── The Dockerfile ───────────────────────────────────────────────────────


def test_the_image_installs_the_agents_package_once_and_from_the_pins() -> None:
    installs = [
        line for line in _instructions() if line.startswith("RUN") and "pip install" in line
    ]
    assert len(installs) == 1, f"one pip install in Dockerfile.agent, found {installs}"
    install = installs[0]
    assert '"./agents${AGENT_EXTRAS:+[$AGENT_EXTRAS]}"' in install, install
    constraint = re.findall(r"(?:^|\s)-c\s+(\S+)", install)
    assert len(constraint) == 1, f"the install passes one constraints file, found {constraint}"
    copies = [line.split() for line in _instructions() if line.startswith("COPY")]
    sources = [parts[1] for parts in copies if parts[-1] == constraint[0]]
    assert sources == [PROVIDERS], (
        f"the file pip is given ({constraint[0]}) must be {PROVIDERS}, copied in; found {sources}"
    )
    order = _instructions()
    assert order.index(f"COPY {PROVIDERS} {constraint[0]}") < order.index(install)


def test_the_constraints_file_reaches_the_build() -> None:
    """A `.dockerignore` rule that left `.github/` out would fail the COPY, and
    one that left only this file out would too: neither may be written."""
    rules = [
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    hiding = [
        rule for rule in rules
        if rule.lstrip("!/").startswith((".github", "**/.github", "*.txt"))
    ]
    assert not hiding, f".dockerignore keeps the pins out of the build: {hiding}"


def test_the_one_file_holds_every_pin_of_the_plain_file() -> None:
    """That is why one `-c` serves an image with or without the provider SDKs."""
    def pin_lines(path: Path) -> set[str]:
        return {
            raw.split(" #", 1)[0].strip()
            for raw in path.read_text(encoding="utf-8").splitlines()
            if raw.strip() and not raw.lstrip().startswith("#")
        }

    plain = pin_lines(REPO_ROOT / ".github" / "python-constraints.txt")
    providers = pin_lines(REPO_ROOT / PROVIDERS)
    assert len(plain) > 50
    assert not sorted(plain - providers), "pins of the plain file that the providers file lacks"


def _overlay_extras() -> dict[str, set[str]]:
    """Each `AGENT_EXTRAS` value a compose file builds an agent image with."""
    found: dict[str, set[str]] = {}
    for path in sorted(REPO_ROOT.glob("docker-compose*.yaml")):
        text = path.read_text(encoding="utf-8")
        for value in re.findall(r"^\s*AGENT_EXTRAS:\s*([\w,-]+)\s*$", text, re.M):
            found.setdefault(path.name, set()).update(value.split(","))
        assert yaml.safe_load(text) is not None
    return found


def test_every_extra_an_overlay_builds_with_is_pinned() -> None:
    with (REPO_ROOT / "agents" / "pyproject.toml").open("rb") as handle:
        extras = tomllib.load(handle)["project"]["optional-dependencies"]
    used = _overlay_extras()
    built_with = {extra for values in used.values() for extra in values}
    assert built_with == {"gemini", "watsonx", "providers"}, used
    pins = _pins()
    for overlay, names in used.items():
        for extra in names:
            for spec in extras[extra]:
                requirement = Requirement(spec)
                if canonicalize_name(requirement.name) == "persatrix-agents":
                    assert requirement.extras <= set(extras), spec  # the bundle of the others
                    continue
                assert canonicalize_name(requirement.name) in pins, (
                    f"{overlay} builds with [{extra}], which needs {requirement.name}: not pinned"
                )


# ─── The make target ──────────────────────────────────────────────────────


def test_the_check_builds_the_image_both_ways_and_compares_both() -> None:
    recipe = [line.strip() for line in makefile_recipe_body("image-pins-check").splitlines()]
    builds = [line for line in recipe if line.startswith("docker build")]
    assert len(builds) == 2, builds
    assert all("-f Dockerfile.agent" in build and build.endswith(" .") for build in builds)
    plain, with_sdks = builds
    assert "AGENT_EXTRAS" not in plain
    assert "--build-arg AGENT_EXTRAS=providers" in with_sdks
    tags = [re.search(r"-t (\S+)", build).group(1) for build in builds]  # type: ignore[union-attr]
    assert len(set(tags)) == 2
    assert recipe[-1] == f"$(PYTHON) scripts/checks/image_pins.py {tags[0]} {tags[1]}", recipe[-1]


# ─── The comparison ───────────────────────────────────────────────────────


def test_a_pin_applies_only_where_its_marker_holds() -> None:
    """numpy has one pin below Python 3.12 and another from 3.12. The check
    runs on the image's own Python so that it reads the one that applies."""
    lines = [
        "# a comment",
        "aiohttp==3.14.5",
        "    # via persatrix-agents",
        "Typing_Extensions==4.15.0",
        "numpy==2.4.6 ; python_full_version < '3'",
        "numpy==2.5.4 ; python_full_version >= '3'",
        "colorama==0.4.6 ; sys_platform == 'no-such-platform'",
    ]
    assert image_pins.applicable_pins(lines) == {
        "aiohttp": "3.14.5", "typing-extensions": "4.15.0", "numpy": "2.5.4",
    }


@pytest.mark.parametrize(
    "lines",
    [
        ["aiohttp>=3.12"],
        ["aiohttp==3.14.5,!=3.14.6"],
        ["aiohttp"],
        ["numpy==2.4.6 ; python_full_version >= '3'", "numpy==2.5.4 ; python_full_version >= '3'"],
    ],
)
def test_a_line_that_is_not_one_exact_pin_is_refused(lines: list[str]) -> None:
    with pytest.raises(ValueError, match="pin"):
        image_pins.applicable_pins(lines)


def test_the_repositorys_own_file_gives_one_pin_per_package_here() -> None:
    text = (REPO_ROOT / PROVIDERS).read_text(encoding="utf-8")
    pins = image_pins.applicable_pins(text.splitlines())
    assert {"aiohttp", "grpcio", "numpy", "google-genai", "ibm-watsonx-ai"} <= set(pins)
    assert len(pins) > 90


def test_the_comparison_names_what_is_not_at_its_pin() -> None:
    pins = {"aiohttp": "3.14.5", "grpcio": "1.84.1", "pytest": "9.0.0"}
    installed = {
        "aiohttp": "3.14.5",          # at its pin
        "grpcio": "1.85.1",           # newer than its pin
        "left-pad": "1.0.0",          # no pin at all
        "pip": "25.0.1",              # the base image's own
        "persatrix-agents": "0.3.16",  # the package the image installs
    }
    assert image_pins.compare(installed, pins) == [
        "grpcio 1.85.1 is installed; the pin is 1.84.1",
        "left-pad 1.0.0 is installed and has no pin",
    ]
    assert image_pins.compare({"aiohttp": "3.14.5", "pip": "25.0.1"}, pins) == []


def test_names_compare_as_pypi_compares_them() -> None:
    assert image_pins.canonical("Typing_Extensions") == "typing-extensions"
    assert image_pins.canonical("ruamel.yaml") == canonicalize_name("ruamel.yaml")
    assert image_pins.canonical("a__b--c") == canonicalize_name("a__b--c")


def test_inside_an_environment_the_check_passes_or_fails_on_that_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that runs in the image, run here on a made-up environment."""
    pins = tmp_path / "pins.txt"
    pins.write_text("aiohttp==3.14.5\ngrpcio==1.84.1\n", encoding="utf-8")

    at_pins = {"aiohttp": "3.14.5", "pip": "25.0.1"}
    monkeypatch.setattr(image_pins, "installed_here", lambda: at_pins)
    assert image_pins.main(["--inside", str(pins)]) == 0
    assert "all 1 installed packages are at their pin" in capsys.readouterr().out

    drifted = {"aiohttp": "3.14.5", "grpcio": "1.85.1"}
    monkeypatch.setattr(image_pins, "installed_here", lambda: drifted)
    assert image_pins.main(["--inside", str(pins)]) == 1
    assert "grpcio 1.85.1 is installed; the pin is 1.84.1" in capsys.readouterr().out
