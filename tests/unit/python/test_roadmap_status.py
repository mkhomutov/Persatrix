"""Pin ``scripts/checks/roadmap_status.py`` — ROADMAP rows behind their RFC.

ROADMAP.md's Component Status tables give each package or module a Status
cell, and most of those cells name the RFC that built it: "✅ Complete
(RFC 0006 PR 1a)". Nothing compared the cell with the RFC, so the
``internal/security/`` row said "🚧 In progress (v0.3.0 — RFC 0009 PRs
1/1b/1c/2/3)" from May to September 2026 while RFC 0009's front-matter said
``partially_implemented``. A row may no longer say less than the RFCs its
Status cell names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import rfcs
from scripts.checks import roadmap_status
from scripts.checks.roadmap_status import (
    BehindRow,
    StatusRow,
    component_status_rows,
    find_behind_rows,
)

STATUSES = {
    "0006": "implemented",
    "0009": "partially_implemented",
    "0018": "implemented",
    "0030": "implementing",
    "0007": "proposed",
}

FIRST_ROW = 9  # the line of the first data row _roadmap() writes


def _roadmap(*cells: str) -> str:
    """A Component Status section laid out as ROADMAP.md lays it out."""
    rows = "".join(f"| `pkg{i}/` | Does a thing | {cell} |\n" for i, cell in enumerate(cells))
    return (
        "## v0.1.0 — Core Engine\n"
        "\n"
        "### Component Status\n"
        "\n"
        "#### Go Orchestrator (`internal/`)\n"
        "\n"
        "| Package | Purpose | Status |\n"
        "|---------|---------|--------|\n"
        f"{rows}"
    )


def _behind(*cells: str) -> list[BehindRow]:
    return find_behind_rows(component_status_rows(_roadmap(*cells)), STATUSES)


def test_the_security_row_that_lagged_rfc_0009_is_flagged() -> None:
    behind = _behind("🚧 In progress (v0.3.0 — RFC 0009 PRs 1/1b/1c/2/3)")
    assert [(b.line, b.component) for b in behind] == [(FIRST_ROW, "`pkg0/`")]
    assert behind[0].rfcs == (("0009", "partially_implemented"),)


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param("🚧 In progress (RFC 0006 PR 6)", id="in-progress-after-full-close"),
        pytest.param("⚠️ Partially Implemented (RFC 0006)", id="partial-after-full-close"),
        pytest.param("⚠ Partially Implemented (RFC 0006)", id="partial-without-u+fe0f"),
        pytest.param("🔲 TODO stub (RFC 0006)", id="stub-after-full-close"),
        pytest.param("📋 Planned (RFC 0006)", id="planned-after-full-close"),
        pytest.param("🚧 In progress (RFC 0009 Phases 1–2)", id="in-progress-after-partial-close"),
        pytest.param("🚧 In progress (RFC 0009 + RFC 0018)", id="every-named-rfc-has-shipped"),
    ],
)
def test_a_row_behind_every_rfc_it_names_is_flagged(cell: str) -> None:
    assert [b.line for b in _behind(cell)] == [FIRST_ROW]


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param("✅ Complete (RFC 0006 PR 1a)", id="complete"),
        pytest.param("✅ Updated (RFC 0006 PR 1a)", id="updated"),
        pytest.param("🚀 Stable (RFC 0006)", id="stable"),
        pytest.param(
            "⚠️ Partially Implemented — RFC 0009 Phases 1–2 shipped in v0.3.0",
            id="partial-close",
        ),
        pytest.param("✅ Complete (RFC 0009 Phase 1 audit logger)", id="done-part-of-partial-rfc"),
        pytest.param("🔲 TODO stub (RFC 0009 Phases 3–4, v0.4.0)", id="deferred-part-of-an-rfc"),
        pytest.param("🚧 In progress (RFC 0030 PR 2)", id="rfc-still-implementing"),
        pytest.param("✅ Complete (RFC 0030 PR 1)", id="done-before-its-rfc"),
        pytest.param("🚧 In progress (RFC 0009 + RFC 0030)", id="one-named-rfc-implementing"),
        pytest.param(
            "⚠️ Partially Implemented (RFC 0009; RFC 0018's redactor)",
            id="partial-and-full-rfcs",
        ),
        pytest.param("✅ Complete (v0.1; RFC 0007 extends it in v0.4.0)", id="names-a-future-rfc"),
        pytest.param("🔲 TODO stub (post-v0.1)", id="names-no-rfc"),
        pytest.param("🔲 TODO stub (RFC 0010, not yet written)", id="names-an-rfc-with-no-file"),
        pytest.param("🚧 In progress (RFC 0009 + RFC 0010)", id="no-file-counts-as-not-started"),
    ],
)
def test_a_row_that_keeps_up_with_its_rfcs_passes(cell: str) -> None:
    assert _behind(cell) == []


def test_the_fix_says_what_the_row_should_say() -> None:
    """A finished RFC wants ✅; one that closed part-way also allows ⚠️ and 🔲."""
    assert _behind("🚧 In progress (RFC 0006 PR 6)")[0].fix == "mark the row ✅"
    assert _behind("🔲 TODO stub (RFC 0006)")[0].fix == "mark the row ✅"
    partial = _behind("🚧 In progress (RFC 0009)")[0].fix
    assert partial == "none of it is in progress; use ✅, ⚠️ or 🔲"


def test_a_stable_rfc_counts_as_finished() -> None:
    rows = component_status_rows(_roadmap("⚠️ Partially Implemented (RFC 0001)"))
    assert [b.fix for b in find_behind_rows(rows, {"0001": "stable"})] == ["mark the row ✅"]


def test_rows_are_read_from_the_status_cell_under_sub_headings() -> None:
    rows = component_status_rows(_roadmap("✅ Complete (RFC 0006 PR 1a)"))
    assert rows == [StatusRow(FIRST_ROW, "`pkg0/`", "✅ Complete (RFC 0006 PR 1a)")]


def test_an_rfc_named_only_outside_the_status_cell_is_not_compared() -> None:
    text = _roadmap("🔲 TODO stub (post-v0.1)").replace("Does a thing", "Replaces RFC 0006's stub")
    assert find_behind_rows(component_status_rows(text), STATUSES) == []


def test_a_table_without_a_status_column_is_skipped() -> None:
    text = (
        "### Component Status\n\n"
        "| Component | Target RFC |\n|-----------|------------|\n| Security | RFC 0009 |\n"
    )
    assert component_status_rows(text) == []


def test_only_component_status_tables_are_compared() -> None:
    """RFC Scope and Planned Components rows track one phase, not the whole RFC."""
    stale = "🚧 In progress (RFC 0009)"
    in_scope = f"| `internal/security/` | Audit logger | {stale} |"
    text = "\n".join(
        [
            "## v0.3.0 — Agent Conversations",
            "",
            "### RFC Scope",
            "",
            "| RFC | Title | Status |",
            "|-----|-------|--------|",
            f"| 0009 | Security | {stale} |",
            "",
            "### Component Status",
            "",
            "#### Go Orchestrator (`internal/`)",
            "",
            "| Package | Purpose | Status |",
            "|---------|---------|--------|",
            in_scope,
            "",
            "### Planned Components (v0.3.0)",
            "",
            "| Component | Go Package | Status |",
            "|-----------|------------|--------|",
            f"| Security | `internal/security/` | {stale} |",
            "",
            "## v0.4.0 — Agent Organizations",
            "",
            "| Package | Purpose | Status |",
            "|---------|---------|--------|",
            f"| `internal/identity/` | Agent tokens | {stale} |",
        ]
    )
    behind = find_behind_rows(component_status_rows(text), STATUSES)
    assert [b.line for b in behind] == [text.splitlines().index(in_scope) + 1]


def _write_rfc(directory: Path, number: str, status: str) -> None:
    (directory / f"{number}-sample.md").write_text(
        "---\n"
        f"id: RFC-{number}\n"
        "title: Sample\n"
        "summary: A sample RFC.\n"
        "type: feature\n"
        f"status: {status}\n"
        "author: Test\n"
        "created: 2026-09-11\n"
        "target: v0.1\n"
        "---\n",
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with one RFC, 0009, closed part-way; the test writes ROADMAP.md."""
    rfcs_dir = tmp_path / "docs" / "rfcs"
    rfcs_dir.mkdir(parents=True)
    _write_rfc(rfcs_dir, "0009", "partially_implemented")
    monkeypatch.setattr(rfcs, "RFCS_DIR", rfcs_dir)
    monkeypatch.setattr(roadmap_status, "ROADMAP_FILE", tmp_path / "ROADMAP.md")
    return tmp_path


def test_the_check_fails_on_a_row_behind_its_rfc(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """This is what the pre-commit hook and CI's Validate configs job run."""
    cell = "🚧 In progress (RFC 0009 PRs 1–3)"
    (repo / "ROADMAP.md").write_text(_roadmap(cell), encoding="utf-8")
    assert roadmap_status.main([]) == 1
    out = capsys.readouterr().out
    assert f"ROADMAP.md:{FIRST_ROW}:" in out
    assert "RFC 0009 is partially_implemented" in out


def test_the_check_passes_once_the_row_catches_up(repo: Path) -> None:
    cell = "⚠️ Partially Implemented (RFC 0009 Phases 1–2)"
    (repo / "ROADMAP.md").write_text(_roadmap(cell), encoding="utf-8")
    assert roadmap_status.main([]) == 0


def test_a_roadmap_with_no_component_status_table_fails_instead_of_passing_empty(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A renamed heading would otherwise leave a check that reads nothing and passes."""
    (repo / "ROADMAP.md").write_text("# Roadmap\n\n## Version Map\n", encoding="utf-8")
    assert roadmap_status.main([]) == 1
    assert "no Component Status table" in capsys.readouterr().out
