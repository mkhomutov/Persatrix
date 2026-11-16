#!/usr/bin/env python3
"""Fail when a package in the agent image is not at the version CI pins.

The agent image installs the agents package with the pins of
``.github/python-constraints-providers.txt`` (``Dockerfile.agent``), so what
ships is what the pins say and moves only when they move (ISSUE-0219).
Before that the image took the newest release of every dependency on the
day it was built, while every CI job tested the pinned ones.

This check holds the image to it. It lists every package installed in the
image and compares each with the pin that applies there:

    python scripts/checks/image_pins.py <image> [<image> ...]

A pin can carry a marker (``numpy==2.5.4 ; python_full_version >= '3.12'``),
and which pin applies depends on the Python and the platform that read it.
So the comparison runs inside the image, on the image's own Python: this
script starts the image with itself and the constraints file mounted read
only, and the copy inside does the reading (``--inside``). The copy inside
uses the standard library and the ``packaging`` that pip carries, nothing
the image would have to install.

It fails on a package that has no pin, and on one whose version is not its
pin. ``pip`` comes with the base image and the agents package is the thing
installed, so neither is expected to have a pin.
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONSTRAINTS = REPO_ROOT / ".github" / "python-constraints-providers.txt"

#: Installed in every image and pinned by nothing: the base image's own
#: installer, and the package the image exists to install.
NOT_PINNED_ON_PURPOSE = frozenset({"pip", "persatrix-agents"})

_INSIDE_SCRIPT = "/image_pins.py"
_INSIDE_PINS = "/python-constraints.txt"


def canonical(name: str) -> str:
    """A package name as PyPI compares names: lower case, runs of ``-_.`` as one ``-``."""
    out: list[str] = []
    for char in name.lower():
        if char in "-_.":
            if out and out[-1] != "-":
                out.append("-")
        else:
            out.append(char)
    return "".join(out)


def _requirement_class() -> Any:
    """The class that parses a requirement line: `packaging`'s, or the copy
    pip carries, which is the one an image is sure to have."""
    for module in ("packaging.requirements", "pip._vendor.packaging.requirements"):
        try:
            return importlib.import_module(module).Requirement
        except ImportError:
            continue
    raise RuntimeError("neither packaging nor pip is installed: cannot read the pins")


def applicable_pins(lines: Iterable[str]) -> dict[str, str]:
    """The pin of each package that applies on the Python running this.

    A line is ``name==version`` with an optional ``; marker``. A line whose
    marker is false here is another Python's or another platform's pin and is
    left out. Anything else in the file is a comment.
    """
    requirement_class = _requirement_class()
    pins: dict[str, str] = {}
    for raw in lines:
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        requirement = requirement_class(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError(f"not a pin of one exact version: {line!r}")
        name = canonical(requirement.name)
        if name in pins:
            raise ValueError(
                f"two pins apply to {name} here: {pins[name]} and {specifiers[0].version}"
            )
        pins[name] = specifiers[0].version
    return pins


def compare(installed: dict[str, str], pins: dict[str, str]) -> list[str]:
    """What is wrong with *installed*, one line per package; empty when nothing is."""
    problems: list[str] = []
    for name in sorted(installed):
        if name in NOT_PINNED_ON_PURPOSE:
            continue
        if name not in pins:
            problems.append(f"{name} {installed[name]} is installed and has no pin")
        elif installed[name] != pins[name]:
            problems.append(f"{name} {installed[name]} is installed; the pin is {pins[name]}")
    return problems


def installed_here() -> dict[str, str]:
    """Every distribution installed for the Python running this."""
    return {canonical(dist.metadata["Name"]): dist.version for dist in metadata.distributions()}


def check_inside(constraints: Path) -> int:
    """The half that runs in the image. Prints its findings; 1 when there are any."""
    installed = installed_here()
    pins = applicable_pins(constraints.read_text(encoding="utf-8").splitlines())
    problems = compare(installed, pins)
    checked = len([name for name in installed if name not in NOT_PINNED_ON_PURPOSE])
    version = ".".join(str(part) for part in sys.version_info[:3])
    if problems:
        print(f"[FAIL] Python {version}: {len(problems)} of {checked} installed packages "
              "are not at their pin:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"[OK] Python {version}: all {checked} installed packages are at their pin "
          f"({len(pins)} pins apply here)")
    return 0


def check_image(image: str) -> int:
    """Run the check inside *image* and pass its verdict on."""
    print(f"== {image}", flush=True)
    command = [
        "docker", "run", "--rm",
        "-v", f"{Path(__file__).resolve()}:{_INSIDE_SCRIPT}:ro",
        "-v", f"{CONSTRAINTS}:{_INSIDE_PINS}:ro",
        "--entrypoint", "python",
        image, _INSIDE_SCRIPT, "--inside", _INSIDE_PINS,
    ]
    return subprocess.run(command, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", nargs="*", help="image tags to check")
    parser.add_argument("--inside", metavar="CONSTRAINTS",
                        help="check the Python running this against a constraints file")
    args = parser.parse_args(argv)
    if args.inside:
        return check_inside(Path(args.inside))
    if not args.images:
        parser.error("name at least one image")
    return max(check_image(image) for image in args.images)


if __name__ == "__main__":
    sys.exit(main())
