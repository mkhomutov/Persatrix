"""Pin ``scripts/release/open_doc.py`` — a version-cycle document from its template.

Copies a template from ``docs/templates/`` to its ``docs/vX.Y.Z-…`` path with
the version, codename, previous version and date filled in and the
``> Guidance:`` blockquotes removed. Refuses to overwrite.

A patch release writes one document, its plan, plus the execution report that
is its evidence (sequencing Amendment 2026-09-12, ruling (e)), so those are
the only two kinds left to open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release.open_doc import KINDS, fill, open_doc

REPO = Path(__file__).resolve().parents[3]

TEMPLATE = """# vX.Y.Z Plan — <Codename>

> Guidance: this file is the release's one document; fill every section
> before the plan PR opens.

**Created**: YYYY-MM-DD · previous `X.Y.(Z-1)` · *<Codename>* · bump `X.Y.(Z-1)` → `X.Y.Z`

> Not guidance — stays.
"""


def test_fill_replaces_every_placeholder_and_drops_guidance() -> None:
    out = fill(TEMPLATE, version="0.3.16", codename="Who is listening",
               previous="0.3.15", today="2026-09-20")
    assert out.startswith("# v0.3.16 Plan — Who is listening")
    assert "Guidance:" not in out
    assert "> Not guidance — stays." in out
    assert (
        "**Created**: 2026-09-20 · previous `0.3.15` · *Who is listening*"
        " · bump `0.3.15` → `0.3.16`"
    ) in out
    assert "X.Y" not in out and "<Codename>" not in out


def test_only_the_plan_and_the_execution_report_can_be_opened() -> None:
    """Scope locks, the release-prep plan, its baseline and the checklist live in the plan now."""
    assert set(KINDS) == {"plan", "execution-report"}


def test_every_kind_maps_to_an_existing_template_and_a_versioned_path() -> None:
    for kind, (template, out_pattern) in KINDS.items():
        assert (REPO / template).is_file(), kind
        assert "{version}" in out_pattern, kind


def test_the_plan_template_opens_with_the_follow_up_and_holds_locks_and_checklist() -> None:
    """The previous release's follow-up comes first; the locks and the checklist sit inside."""
    template = (REPO / KINDS["plan"][0]).read_text(encoding="utf-8")
    out = fill(template, version="0.3.17", codename="c", previous="0.3.16", today="2026-10-01")
    headings = [line for line in out.splitlines() if line.startswith("## ")]
    assert headings[0] == "## v0.3.16 follow-up"
    assert "## Scope locks" in headings
    assert "## Release checklist" in headings


def test_open_doc_writes_the_target_and_refuses_to_overwrite(tmp_path: Path) -> None:
    (tmp_path / "docs" / "templates").mkdir(parents=True)
    (tmp_path / "docs" / "templates" / "VERSION_PLAN_TEMPLATE.md").write_text(
        TEMPLATE, encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("## [0.3.15] - 2026-09-09\n", encoding="utf-8")

    path = open_doc(tmp_path, kind="plan", version="0.3.16",
                    codename="Who is listening", today="2026-09-20")

    assert path == tmp_path / "docs" / "v0.3.16-plan.md"
    text = path.read_text(encoding="utf-8")
    assert "previous `0.3.15`" in text  # previous version read from the changelog
    with pytest.raises(FileExistsError):
        open_doc(tmp_path, kind="plan", version="0.3.16", codename="x", today="2026-09-20")


def test_fill_handles_the_branch_prefix_next_patch_and_sequencing_placeholders() -> None:
    text = (
        "**Branch prefix**: `feature/vXYZ-`\n"
        "- **The vX.Y.(Z+1) bundle** and X.Y.(Z+1)\n"
        "ratified by `docs/v<line>.x-sequencing.md` (the current line's sequencing doc)"
        " §Amendment YYYY-MM-DD\n"
    )
    out = fill(text, version="0.3.16", codename="c", previous="0.3.15", today="2026-09-20")
    assert "`feature/v0316-`" in out
    assert "The v0.3.17 bundle** and 0.3.17" in out
    assert "`docs/v0.3.x-sequencing.md` §Amendment YYYY-MM-DD" in out  # amendment date stays


def test_fill_rejects_a_version_that_is_not_x_y_z() -> None:
    with pytest.raises(ValueError):
        fill("x", version="0.4", codename="c", previous="0.3.16", today="2026-09-20")


def test_open_doc_refuses_when_no_older_release_exists(tmp_path: Path) -> None:
    (tmp_path / "docs" / "templates").mkdir(parents=True)
    (tmp_path / "docs" / "templates" / "VERSION_PLAN_TEMPLATE.md").write_text(
        TEMPLATE, encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pass --previous"):
        open_doc(tmp_path, kind="plan", version="0.1.0", codename="x", today="2026-09-20")
