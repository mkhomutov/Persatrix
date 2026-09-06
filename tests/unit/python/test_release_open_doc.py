"""Pin ``scripts/release/open_doc.py`` — a version-cycle document from its template.

Copies a template from ``docs/templates/`` to its ``docs/vX.Y.Z-…`` path with
the version, codename, previous version and date filled in and the
``> Guidance:`` blockquotes removed. Refuses to overwrite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release.open_doc import KINDS, fill, open_doc

TEMPLATE = """# vX.Y.Z Release Checklist

> Guidance: copy the previous release's checklist and the release baseline's
> "differs from last release" list side by side.

**Created**: YYYY-MM-DD · previous `X.Y.(Z-1)` · *<Codename>* · bump `X.Y.(Z-1)` → `X.Y.Z`

> Not guidance — stays.
"""


def test_fill_replaces_every_placeholder_and_drops_guidance() -> None:
    out = fill(TEMPLATE, version="0.3.16", codename="Who is listening",
               previous="0.3.15", today="2026-09-20")
    assert out.startswith("# v0.3.16 Release Checklist")
    assert "Guidance:" not in out
    assert "> Not guidance — stays." in out
    assert (
        "**Created**: 2026-09-20 · previous `0.3.15` · *Who is listening*"
        " · bump `0.3.15` → `0.3.16`"
    ) in out
    assert "X.Y" not in out and "<Codename>" not in out


def test_every_kind_maps_to_an_existing_template_and_a_versioned_path() -> None:
    repo = Path(__file__).resolve().parents[3]
    for kind, (template, out_pattern) in KINDS.items():
        assert (repo / template).is_file(), kind
        assert "{version}" in out_pattern, kind


def test_open_doc_writes_the_target_and_refuses_to_overwrite(tmp_path: Path) -> None:
    (tmp_path / "docs" / "templates").mkdir(parents=True)
    (tmp_path / "docs" / "templates" / "RELEASE_CHECKLIST_TEMPLATE.md").write_text(
        TEMPLATE, encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("## [0.3.15] - 2026-09-09\n", encoding="utf-8")

    path = open_doc(tmp_path, kind="release-checklist", version="0.3.16",
                    codename="Who is listening", today="2026-09-20")

    assert path == tmp_path / "docs" / "v0.3.16-release-checklist.md"
    text = path.read_text(encoding="utf-8")
    assert "previous `0.3.15`" in text  # previous version read from the changelog
    with pytest.raises(FileExistsError):
        open_doc(tmp_path, kind="release-checklist", version="0.3.16",
                 codename="x", today="2026-09-20")


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
    (tmp_path / "docs" / "templates" / "RELEASE_CHECKLIST_TEMPLATE.md").write_text(
        TEMPLATE, encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pass --previous"):
        open_doc(tmp_path, kind="release-checklist", version="0.1.0", codename="x",
                 today="2026-09-20")
