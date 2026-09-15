"""Pin the web console's declared Node range to what actually installs.

`web/.npmrc` sets `engine-strict`, so `npm ci` refuses to install on a Node
that any locked package does not accept. `web/package.json` tells contributors
which Node they need. The two drifted: the web dependency bump in #867 brought
jsdom 30, which needs Node `^22.22.2 || ^24.15.0 || >=26.0.0`, while
`package.json` still said `>=20`. A contributor on Node 20, or on Node 22.16,
was told they were supported and then `make ui` refused before building.

These tests keep the declared range inside every locked package's own range.
They also keep CI, the Docker image, `.npmrc` and the contributor docs on it.
The small range reader below understands only the forms the lockfile uses and
raises on anything else, so a new form fails loudly instead of being skipped.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from _test_infra import REPO_ROOT, ci_job_steps

WEB = REPO_ROOT / "web"

Version = tuple[int, int, int]
# A half-open run of versions [low, high); ``None`` means no upper end.
Interval = tuple[Version, Version | None]

_COMPARATOR = re.compile(r"(>=|<=|>|<|=|\^|~)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _parse_comparator(text: str) -> Interval:
    m = _COMPARATOR.fullmatch(text)
    if m is None:
        raise ValueError(f"unreadable Node range part {text!r}")
    op, major_s, minor_s, patch_s = m.groups()
    major = int(major_s)
    minor = int(minor_s) if minor_s is not None else None
    patch = int(patch_s) if patch_s is not None else None
    low: Version = (major, minor or 0, patch or 0)
    # The first version past everything the written parts name: 20 → 21.0.0,
    # 20.1 → 20.2.0, 20.1.2 → 20.1.3.
    if minor is None:
        past: Version = (major + 1, 0, 0)
    elif patch is None:
        past = (major, minor + 1, 0)
    else:
        past = (major, minor, patch + 1)

    if op == ">=":
        return low, None
    if op == ">":
        return past, None
    if op == "<":
        return (0, 0, 0), low
    if op == "<=":
        return (0, 0, 0), past
    if op in (None, "="):
        return low, past
    if op == "^":
        # Caret keeps the left-most non-zero part fixed.
        if major > 0 or minor is None:
            return low, (major + 1, 0, 0)
        if minor > 0 or patch is None:
            return low, (0, minor + 1, 0)
        return low, (0, 0, patch + 1)
    # Tilde keeps major and minor fixed, or only major when minor is absent.
    return low, ((major + 1, 0, 0) if minor is None else (major, minor + 1, 0))


def parse_node_range(text: str) -> list[Interval]:
    """Read an npm ``engines.node`` range as a union of version intervals."""
    intervals: list[Interval] = []
    for alternative in text.split("||"):
        alternative = alternative.strip()
        if alternative in ("", "*"):
            intervals.append(((0, 0, 0), None))
            continue
        # Join an operator to its version (`>= 0.4`), then split the AND-parts.
        parts = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", alternative).split()
        low: Version = (0, 0, 0)
        high: Version | None = None
        for part in parts:
            part_low, part_high = _parse_comparator(part)
            low = max(low, part_low)
            if part_high is not None:
                high = part_high if high is None else min(high, part_high)
        if high is None or low < high:
            intervals.append((low, high))
    return intervals


def _merged(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for low, high in sorted(intervals, key=lambda iv: iv[0]):
        if merged:
            last_low, last_high = merged[-1]
            if last_high is None or low <= last_high:
                if last_high is not None and (high is None or high > last_high):
                    merged[-1] = (last_low, high)
                continue
        merged.append((low, high))
    return merged


def admits(text: str, version: str) -> bool:
    """True when ``version`` (``X.Y.Z``) satisfies the range ``text``."""
    v = tuple(int(p) for p in version.split("."))
    return any(lo <= v and (hi is None or v < hi) for lo, hi in parse_node_range(text))


def no_looser_than(declared: str, required: str) -> bool:
    """True when every Node ``declared`` admits is also admitted by ``required``."""
    allowed = _merged(parse_node_range(required))
    for low, high in parse_node_range(declared):
        inside = False
        for a_low, a_high in allowed:
            if a_low <= low and (a_high is None or (high is not None and high <= a_high)):
                inside = True
                break
        if not inside:
            return False
    return True


def _declared() -> str:
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    return str(package["engines"]["node"])


def _locked_packages() -> dict[str, dict[str, Any]]:
    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    return dict(lock["packages"])


JSDOM_30 = "^22.22.2 || ^24.15.0 || >=26.0.0"


@pytest.mark.parametrize(
    ("text", "version", "expected"),
    [
        (">=20", "20.0.0", True),
        (">=20", "19.9.9", False),
        (">= 0.4", "0.4.0", True),
        (">=v12.22.7", "12.22.6", False),
        ("20 || >=22", "21.0.0", False),
        ("20 || >=22", "20.9.1", True),
        ("^13.7", "13.6.9", False),
        ("^20.19 || ^22.12 || >=24", "22.11.0", False),
        ("^0.4.2", "0.5.0", False),
        ("~1.2.3", "1.2.9", True),
        ("~1.2.3", "1.3.0", False),
        (">=18 <20", "20.0.0", False),
        ("<=20.1", "20.1.9", True),
        (JSDOM_30, "20.19.0", False),
        (JSDOM_30, "22.16.0", False),
        (JSDOM_30, "22.22.2", True),
        (JSDOM_30, "23.0.0", False),
        (JSDOM_30, "24.14.9", False),
        (JSDOM_30, "24.15.0", True),
        (JSDOM_30, "25.9.0", False),
        (JSDOM_30, "26.0.0", True),
    ],
)
def test_range_reader_reads_the_forms_the_lockfile_uses(
    text: str, version: str, expected: bool
) -> None:
    assert admits(text, version) is expected


@pytest.mark.parametrize("text", [">=18.0.0-0", "latest", "1.2.3 - 2.3.4", "20.x"])
def test_range_reader_refuses_forms_it_does_not_understand(text: str) -> None:
    with pytest.raises(ValueError, match="unreadable"):
        parse_node_range(text)


@pytest.mark.parametrize(
    ("declared", "required", "expected"),
    [
        (JSDOM_30, ">=20", True),
        (">=20", JSDOM_30, False),
        (JSDOM_30, "^22.12.0 || ^24.0.0 || >=26.0.0", True),
        (JSDOM_30, "^20.19.0 || >=22.12.0", True),
        ("^22.12.0 || >=24.0.0", JSDOM_30, False),
        (">=22", "^22.0.0 || ^23.0.0 || >=24", True),
    ],
)
def test_no_looser_than(declared: str, required: str, expected: bool) -> None:
    assert no_looser_than(declared, required) is expected


def test_declared_range_is_no_looser_than_jsdom() -> None:
    """The requirement that broke `make ui` on Node 22.16, pinned by name."""
    jsdom = _locked_packages().get("node_modules/jsdom")
    assert jsdom is not None, "jsdom is no longer locked; re-point this pin"
    required = jsdom["engines"]["node"]
    declared = _declared()
    assert no_looser_than(declared, required), (
        f"web/package.json engines.node {declared!r} admits Node versions "
        f"jsdom {jsdom['version']} refuses ({required!r})"
    )
    for refused in ("20.19.0", "22.16.0"):
        assert not admits(declared, refused), f"declared range still admits Node {refused}"


def _looser_locked_packages(
    declared: str, packages: dict[str, dict[str, Any]]
) -> tuple[int, list[str]]:
    """Count the locked packages that state ``engines.node``, and list the ones
    that refuse a Node ``declared`` admits. A range the reader cannot read
    raises, naming the package that brought it."""
    checked = 0
    looser: list[str] = []
    for path, meta in packages.items():
        node = (meta.get("engines") or {}).get("node")
        if path == "" or node is None:
            continue  # "" is web/package.json itself, mirrored into the lock
        checked += 1
        name = f"{path.split('node_modules/')[-1]}@{meta.get('version')}"
        try:
            parse_node_range(node)
        except ValueError as err:
            raise ValueError(f"{name}: {err}") from err
        if not no_looser_than(declared, node):
            looser.append(f"{name}: {node}")
    return checked, looser


def test_declared_range_is_no_looser_than_any_locked_package() -> None:
    declared = _declared()
    checked, looser = _looser_locked_packages(declared, _locked_packages())
    assert checked > 20, f"only {checked} locked packages declare engines.node — lockfile misread?"
    assert not looser, (
        f"web/package.json engines.node {declared!r} admits Node versions these "
        f"locked packages refuse under engine-strict: {looser}"
    )


def test_lockfile_root_mirrors_package_json_engines() -> None:
    root = _locked_packages()[""]
    assert root.get("engines", {}).get("node") == _declared(), (
        "web/package-lock.json's root entry disagrees with web/package.json engines.node"
    )


def test_an_unreadable_locked_range_names_its_package() -> None:
    """A dependency bump can bring a range form the reader does not know.

    The failure then says which package brought it, so nobody has to search
    the lockfile for the range text.
    """
    packages = {"node_modules/old-pkg": {"version": "1.0.0", "engines": {"node": "0.10.x"}}}
    with pytest.raises(ValueError, match=r"old-pkg@1\.0\.0: unreadable"):
        _looser_locked_packages(JSDOM_30, packages)


@pytest.mark.parametrize(
    "relpath",
    [
        "web/.npmrc",
        "CONTRIBUTING.md",
        "docs/manual-tests/MT-CONSOLE-001.md",
        "docs/manual-tests/MT-CONSOLE-002.md",
    ],
)
def test_contributor_facing_text_states_the_declared_range(relpath: str) -> None:
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert _declared() in text, f"{relpath} does not state the Node range {_declared()!r}"


def _floor() -> str:
    """The lowest Node version the declared range admits, as ``X.Y.Z``."""
    return ".".join(map(str, min(low for low, _ in parse_node_range(_declared()))))


def test_ci_web_console_job_installs_on_the_declared_floor() -> None:
    """CI builds on the lowest Node the range admits.

    A dependency bump that raises the requirement past it then fails `npm ci`
    in that job too, not only on a contributor's machine.
    """
    steps = ci_job_steps("web-console")
    setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert len(setup) == 1, "the web-console job must set up Node exactly once"
    assert str(setup[0]["with"]["node-version"]) == _floor()


_UI_BUILDER = re.compile(r"^FROM\s+node:(\S+?)-alpine\s+AS\s+ui-builder\s*$", re.M)


def test_docker_ui_builder_installs_on_the_declared_floor() -> None:
    """The image's own `npm ci` runs on the same Node as CI.

    A floating `node:22-alpine` means whatever copy the host has cached, and
    `docker compose build` does not fetch a newer one. A copy older than the
    floor refuses the install, and the Docker path is the one the web console
    guide says needs no Node on the host.
    """
    text = (REPO_ROOT / "Dockerfile.orchestrator").read_text(encoding="utf-8")
    m = _UI_BUILDER.search(text)
    assert m, "Dockerfile.orchestrator has no `FROM node:<version>-alpine AS ui-builder` stage"
    assert m.group(1) == _floor(), (
        f"the ui-builder stage builds on node:{m.group(1)}-alpine, not the declared "
        f"floor node:{_floor()}-alpine"
    )
