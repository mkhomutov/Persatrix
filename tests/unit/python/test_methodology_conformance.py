"""Pin ``scripts/checks/methodology_conformance.py`` and the manifest it reads.

The manifest is the methodology's contract with a repository that adopts it.
Two things must always hold here: this checkout conforms to its own
manifest, and the checker really reports what is missing.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.checks.methodology_conformance import (
    MANIFEST,
    ci_job_names,
    find_missing,
    load_manifest,
    make_targets,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_this_checkout_conforms_to_its_own_manifest() -> None:
    assert find_missing(REPO_ROOT, load_manifest(MANIFEST)) == []


def test_the_manifest_names_the_checker_itself() -> None:
    """The contract includes the tool that enforces it, so a consumer cannot drop it silently."""
    manifest = load_manifest(MANIFEST)
    assert "scripts/checks/methodology_conformance.py" in manifest["tooling"]
    assert "scripts/checks/methodology_conformance.py" in manifest["ci_steps_in_docs_hygiene"]


def test_make_targets_and_ci_job_names_are_parsed() -> None:
    assert make_targets("all: build\n.PHONY: all\nlint-go:\n\tgo vet\n") == {"all", "lint-go"}
    assert ci_job_names("jobs:\n  go:\n    name: Go (build + test)\n    runs-on: x\n") == {
        "Go (build + test)"
    }


def test_missing_items_are_reported_by_kind(tmp_path: Path) -> None:
    manifest = {
        "documents": ["docs/methodology/README.md"],
        "tooling": ["scripts/pre_commit.py"],
        "make_targets": ["lint"],
        "ci_jobs": ["Docs hygiene"],
        "ci_steps_in_docs_hygiene": ["scripts/checks/doc_links.py"],
    }
    (tmp_path / "docs" / "methodology").mkdir(parents=True)
    (tmp_path / "docs" / "methodology" / "README.md").write_text("# m\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("lint:\n\techo\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  docs-hygiene:\n    name: Docs hygiene\n    steps:\n      - run: python x.py\n",
        encoding="utf-8",
    )

    missing = find_missing(tmp_path, manifest)

    assert missing == [
        "tooling: scripts/pre_commit.py",
        "docs-hygiene step: scripts/checks/doc_links.py",
    ]


def test_manifest_is_valid_json_with_the_expected_sections() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("documents", "tooling", "make_targets", "ci_jobs", "ci_steps_in_docs_hygiene"):
        assert isinstance(data[key], list) and data[key], key
