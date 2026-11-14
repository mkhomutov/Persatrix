"""Hold Dependabot's two image entries to what their comments promise.

Dependabot had no entry for a Dockerfile or a compose file, so a base image
had no update path at all (ISSUE-0218). These tests keep two things true of
`.github/dependabot.yml`: every directory that holds a Dockerfile or a
compose file is read by an entry, and no `docker` entry offers a move of the
two image tags that must not move on their own.

The first is written against any config and any set of files, so the tests
below also run the three ways a later change can cover a new directory: a
second entry, a `directories:` list, and a directory whose name starts with
a dot. Each of those once failed here for a reason of the test's own.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from _test_infra import REPO_ROOT

DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

# What makes a file one of an updater's: its name, by the pattern copied from
# dependabot-core (docker/lib/dependabot/docker/file_fetcher.rb and
# docker/lib/dependabot/docker_compose/file_fetcher.rb) as of 2026-11-14, and
# a line only such a file has. The names alone are loose: a test called
# test_dockerfile_….py matches the first.
UPDATERS = {
    "docker": (
        re.compile(r"dockerfile|containerfile", re.IGNORECASE),
        re.compile(r"^FROM[ \t]+\S", re.MULTILINE),
    ),
    "docker-compose": (
        re.compile(r"(docker-)?compose(-[\w]+)?(\.[\w-]+)?\.ya?ml", re.IGNORECASE),
        re.compile(r"^services:", re.MULTILINE),
    ),
}
ECOSYSTEMS = sorted(UPDATERS)


def _config() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    return config


def _entries(config: dict[str, Any], ecosystem: str) -> list[dict[str, Any]]:
    return [u for u in config["updates"] if u["package-ecosystem"] == ecosystem]


def _tracked_files() -> dict[str, str]:
    """Every tracked file an image updater could read, with its text."""
    done = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    names = [
        f
        for f in done.stdout.splitlines()
        if any(name_re.search(Path(f).name) for name_re, _ in UPDATERS.values())
    ]
    return {f: (REPO_ROOT / f).read_text(encoding="utf-8") for f in names}


def _folder(path: str) -> str:
    """The folder of a tracked file as an entry names it, less its slashes: "" is the root."""
    parent = str(Path(path).parent)
    return "" if parent == "." else parent


def _read_folders(entries: list[dict[str, Any]]) -> list[str]:
    """The folders the entries read: each `directory`, and each of `directories`."""
    named: list[str] = []
    for entry in entries:
        if "directory" in entry:
            named.append(str(entry["directory"]))
        named.extend(str(d) for d in entry.get("directories", []))
    return [d.strip("/") for d in named]


def _unread_folders(ecosystem: str, config: dict[str, Any], files: dict[str, str]) -> set[str]:
    """The folders holding one of the updater's files that no entry reads."""
    name_re, line_re = UPDATERS[ecosystem]
    holding = {
        _folder(f)
        for f, text in files.items()
        if name_re.search(Path(f).name) and line_re.search(text)
    }
    read = _read_folders(_entries(config, ecosystem))
    # `directories:` takes `*` and `**`; fnmatch reads both as "any run of
    # characters", which is wider than Dependabot for a lone `*` and is the
    # right way round for a test that must not refuse a correct config.
    return {d for d in holding if not any(fnmatch.fnmatchcase(d, pattern) for pattern in read)}


def _with_entry(ecosystem: str, **keys: Any) -> dict[str, Any]:
    """The real config plus one more entry for an ecosystem, copied from its first."""
    config = _config()
    (first, *_) = _entries(config, ecosystem)
    extra = {k: v for k, v in first.items() if k not in ("directory", "directories")}
    config["updates"].append({**extra, **keys})
    return config


@pytest.mark.parametrize("ecosystem", ECOSYSTEMS)
def test_dependabot_reads_every_directory_that_holds_an_image_reference(ecosystem: str) -> None:
    files = _tracked_files()
    name_re, _ = UPDATERS[ecosystem]
    assert any(name_re.search(Path(f).name) for f in files), (
        f"no tracked file matches the {ecosystem} updater's file names"
    )
    missing = _unread_folders(ecosystem, _config(), files)
    assert not missing, (
        f"Dependabot has no {ecosystem} entry for: {sorted(d or '/' for d in missing)}"
    )


NEW_FILES = {
    "docker": ("Dockerfile", "FROM alpine:3.20\n"),
    "docker-compose": ("compose.yaml", "services:\n  x:\n    image: alpine:3.20\n"),
}


@pytest.mark.parametrize("ecosystem", ECOSYSTEMS)
@pytest.mark.parametrize("folder", ["deploy", ".devcontainer", "deploy/images"])
def test_a_file_in_a_folder_no_entry_reads_is_named(ecosystem: str, folder: str) -> None:
    name, text = NEW_FILES[ecosystem]
    files = {**_tracked_files(), f"{folder}/{name}": text}
    assert _unread_folders(ecosystem, _config(), files) == {folder}


@pytest.mark.parametrize("ecosystem", ECOSYSTEMS)
@pytest.mark.parametrize("folder", ["deploy", ".devcontainer"])
def test_each_way_of_covering_a_new_folder_passes(ecosystem: str, folder: str) -> None:
    """The remedies the failure above asks for: a second entry, or a list."""
    name, text = NEW_FILES[ecosystem]
    files = {**_tracked_files(), f"{folder}/{name}": text}

    second_entry = _with_entry(ecosystem, directory=f"/{folder}")
    assert _unread_folders(ecosystem, second_entry, files) == set()

    listed = _config()
    (entry,) = _entries(listed, ecosystem)
    del entry["directory"]
    entry["directories"] = ["/", f"/{folder}"]
    assert _unread_folders(ecosystem, listed, files) == set()


def _convention_faults(config: dict[str, Any], ecosystem: str) -> list[str]:
    faults = []
    for entry in _entries(config, ecosystem):
        where = entry.get("directory", entry.get("directories"))
        if entry.get("schedule") != {"interval": "monthly"}:
            faults.append(f"{where}: not monthly")
        if entry.get("commit-message", {}).get("prefix") != "chore(deps)":
            faults.append(f"{where}: titles do not start chore(deps)")
        if [g.get("patterns") for g in entry.get("groups", {}).values()] != [["*"]]:
            faults.append(f"{where}: not one grouped PR for every image")
    return faults


@pytest.mark.parametrize("ecosystem", ECOSYSTEMS)
def test_the_image_entries_follow_the_files_conventions(ecosystem: str) -> None:
    assert _entries(_config(), ecosystem), f"Dependabot has no {ecosystem} entry"
    assert _convention_faults(_config(), ecosystem) == []


def _tag_faults(config: dict[str, Any]) -> list[str]:
    """What is wrong with the two tags' rules, in any `docker` entry.

    Every entry is held to them, not only the one that reads the two
    Dockerfiles today: an entry without the rules offers the moves as soon as
    a Dockerfile it reads uses either image.
    """
    faults = []
    for entry in _entries(config, "docker"):
        where = entry.get("directory", entry.get("directories"))
        rules = {r["dependency-name"]: r for r in entry.get("ignore", [])}
        # `python:3.12-slim` has no patch part, so minor and major are every
        # move there is; which Python the image runs is an open choice
        # (ISSUE-0219).
        if set(rules.get("python", {}).get("update-types", [])) != {
            "version-update:semver-major",
            "version-update:semver-minor",
        }:
            faults.append(f"{where}: a Python minor or major move is offered")
        # A rule with a name alone ignores every version. The Node tag is
        # held to the engines floor by test_web_node_engines_floor.py.
        if set(rules.get("node", {"missing": True})) != {"dependency-name"}:
            faults.append(f"{where}: a Node move is offered")
    return faults


def test_dependabot_offers_no_move_of_the_agent_images_python_or_the_node_floor() -> None:
    assert _entries(_config(), "docker"), "Dependabot has no docker entry"
    assert _tag_faults(_config()) == []


def test_a_second_docker_entry_is_held_to_the_same_two_rules() -> None:
    copied = _with_entry("docker", directory="/deploy")
    assert _tag_faults(copied) == []

    bare = _config()
    bare["updates"].append({"package-ecosystem": "docker", "directory": "/deploy"})
    assert _tag_faults(bare) == [
        "/deploy: a Python minor or major move is offered",
        "/deploy: a Node move is offered",
    ]
