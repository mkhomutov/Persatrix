"""Pin the Rust toolchain CI builds, lints and tests the CLI with.

CI used to install whatever Rust `stable` was that day, and the Rust job runs
`cargo clippy -- -D warnings`. So each Rust release (one every six weeks) could
add a lint that failed every open PR at once, whatever the PR changed; clippy
1.88, for one, turned `uninlined_format_args` on by default, and 1.89 turned it
off again. The toolchain now comes from `cli/rust-toolchain.toml`, which
Dependabot bumps: a release reaches CI as one PR, and a new lint fails that PR
alone.

These tests pin the parts that make that true: the file names an exact
release, every workflow job that runs cargo installs what the file names and
runs cargo where the file applies, no workflow names a toolchain of its own
(except the minimum-Rust build, which reads its release from cli/Cargo.toml),
and Dependabot watches the file.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLCHAIN_FILE = REPO_ROOT / "cli" / "rust-toolchain.toml"
GITHUB = REPO_ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"

# Run in cli/, this installs exactly what cli/rust-toolchain.toml names,
# components included, without rustup updating itself mid-job.
INSTALL_FROM_FILE = "rustup toolchain install --no-self-update"
CARGO_RE = re.compile(r"\bcargo\b")
# A workflow choosing its own toolchain: an installer action, or a rustup /
# cargo command that names one.
TOOLCHAIN_ACTIONS = (
    "dtolnay/rust-toolchain",
    "actions-rs/toolchain",
    "actions-rust-lang/setup-rust-toolchain",
)
NAMED_TOOLCHAIN_RE = re.compile(r"rustup\s+(?:default|override)\b|\bcargo\s+[\"']?\+")
RUSTUP_INSTALL_RE = re.compile(r"rustup\s+toolchain\s+install\b(?P<args>[^\n]*)")
# The one step allowed to name a toolchain: it builds the CLI on the oldest
# Rust it supports, `rust-version` in cli/Cargo.toml, which
# test_toolchain_minimums.py ties to the minimum README states. The step reads
# that release from the manifest when it runs, so the workflow names no version.
MINIMUM_RUST_STEP = "Check the CLI compiles on its minimum Rust (rust-version)"
READS_RUST_VERSION = (
    "cargo metadata --no-deps --format-version 1 | jq -er '.packages[0].rust_version'"
)


def _toolchain() -> dict[str, Any]:
    text = TOOLCHAIN_FILE.read_text(encoding="utf-8")
    return dict(tomllib.loads(text)["toolchain"])


def _jobs() -> list[tuple[str, dict[str, Any]]]:
    """(`workflow:job`, job) for every job in every workflow."""
    jobs = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            jobs.append((f"{path.name}:{job_id}", job))
    return jobs


def _runs_cargo(step: dict[str, Any]) -> bool:
    return CARGO_RE.search(str(step.get("run", ""))) is not None


def test_cli_pins_an_exact_rust_release() -> None:
    """`stable` moves with every release, and Dependabot never bumps it; a
    `1.NN` channel still moves with that release's point releases. Only an
    exact `1.NN.P` holds still until a reviewed PR moves it."""
    toolchain = _toolchain()
    channel = str(toolchain.get("channel", ""))
    assert re.fullmatch(r"\d+\.\d+\.\d+", channel), (
        f"channel {channel!r} is not an exact release such as 1.98.1"
    )
    assert {"clippy", "rustfmt"} <= set(toolchain.get("components", [])), (
        "CI runs clippy and rustfmt, so the file must install them"
    )
    assert toolchain.get("profile") == "minimal", "CI needs no docs; keep the install small"


def test_every_job_that_runs_cargo_installs_the_pinned_toolchain_first() -> None:
    rust_jobs = []
    for name, job in _jobs():
        steps = list(job.get("steps") or [])
        cargo_at = [i for i, step in enumerate(steps) if _runs_cargo(step)]
        if not cargo_at:
            continue
        rust_jobs.append(name)
        installs = [
            i
            for i, step in enumerate(steps)
            if str(step.get("run", "")).strip() == INSTALL_FROM_FILE
            and step.get("working-directory") == "cli"
        ]
        assert len(installs) == 1, (
            f"{name} runs cargo, so it must install the toolchain once, from cli/, "
            f"with `{INSTALL_FROM_FILE}`"
        )
        assert installs[0] < cargo_at[0], f"{name} runs cargo before installing the toolchain"
    assert len(rust_jobs) >= 3, (
        f"expected the rust, licenses and weekly audit jobs, found {rust_jobs}"
    )


def test_cargo_runs_in_cli_where_the_toolchain_file_applies() -> None:
    """rustup picks the toolchain from the working directory, not from the
    manifest. A cargo command run anywhere else gets the runner image's own
    Rust, which moves whenever the image does."""
    for name, job in _jobs():
        for step in job.get("steps") or []:
            if not _runs_cargo(step) or step.get("working-directory") == "cli":
                continue
            for line in str(step["run"]).splitlines():
                if CARGO_RE.search(line):
                    assert line.strip().startswith("cd cli && "), (
                        f"{name}: `{line.strip()}` runs cargo outside cli/"
                    )


# `rustup toolchain install` options followed by a value, not a toolchain.
RUSTUP_VALUE_OPTIONS = {"--profile", "-c", "--component", "-t", "--target"}


def _toolchains_installed(args: str) -> list[str]:
    """The toolchain names a `rustup toolchain install` argument list gives."""
    names, words = [], iter(args.split())
    for word in words:
        if word in RUSTUP_VALUE_OPTIONS:
            next(words, None)
        elif not word.startswith("-"):
            names.append(word)
    return names


def _names_a_toolchain(run: str) -> str | None:
    """The first command in `run` that picks a toolchain itself, or None."""
    named = NAMED_TOOLCHAIN_RE.search(run)
    if named:
        return named.group(0)
    for install in RUSTUP_INSTALL_RE.finditer(run):
        if _toolchains_installed(install.group("args")):
            return install.group(0).strip()
    return None


def test_no_workflow_names_a_rust_toolchain_of_its_own() -> None:
    """Dependabot rewrites the version in cli/rust-toolchain.toml and nowhere
    else, so a version, or `stable`, named in a workflow would not move with
    it. The minimum-Rust build is the one exception, and names no version."""
    version = str(_toolchain()["channel"])
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        for action in TOOLCHAIN_ACTIONS:
            assert action not in text, f"{path.name} installs Rust with {action}"
        assert version not in text, f"{path.name} repeats the pinned version {version}"
    for name, job in _jobs():
        for step in job.get("steps") or []:
            if step.get("name") == MINIMUM_RUST_STEP:
                continue
            named = _names_a_toolchain(str(step.get("run", "")))
            assert named is None, f"{name} names a toolchain: `{named}`"


def test_the_minimum_rust_step_reads_its_release_from_the_manifest() -> None:
    """The exception above holds only while the step takes its release from
    `rust-version` and runs in cli/, where rustup would otherwise apply the pin."""
    steps = [
        (name, step)
        for name, job in _jobs()
        for step in job.get("steps") or []
        if step.get("name") == MINIMUM_RUST_STEP
    ]
    assert [name for name, _ in steps] == ["ci.yml:rust"], (
        f"expected the minimum-Rust step once, in the rust job; found {steps}"
    )
    step = steps[0][1]
    run = str(step["run"])
    assert step.get("working-directory") == "cli"
    assert f"msrv=$({READS_RUST_VERSION})" in run, "the step must read rust-version, not name one"
    assert re.findall(r"\+[^\s\"']+", run) == ["+$msrv"], "cargo must run on the release it read"
    installs = [_toolchains_installed(m.group("args")) for m in RUSTUP_INSTALL_RE.finditer(run)]
    assert installs == [['"$msrv"']], "rustup must install only the release it read"


def test_dependabot_proposes_each_rust_release_for_the_file() -> None:
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    blocks = [u for u in config["updates"] if u.get("package-ecosystem") == "rust-toolchain"]
    assert len(blocks) == 1, "expected one Dependabot `rust-toolchain` block"
    block = blocks[0]
    assert block.get("directory") == "/cli", "the block must watch cli/, where the file lives"
    assert (block.get("commit-message") or {}).get("prefix") == "chore(deps)", (
        "the PR title check accepts `chore(deps)`"
    )
