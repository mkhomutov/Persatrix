"""Pin ``scripts/checks/amendment_evidence.py`` — the External evidence gate.

The Amendment 2026-09-12 made every sequencing amendment open with an
**External evidence since the last amendment** section (decisions.md rule 6):
who outside the project installed it, filed anything, saw a demo, talked to
the maintainer, or read a published result. The rule was written down, and
this project keeps its gates but forgets its priorities, so the ruling asked
for a check: an amendment from that date on without the section, or with one
of its fields left blank, fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.checks import amendment_evidence
from scripts.checks.amendment_evidence import (
    REQUIRED_FIELDS,
    amendments,
    evidence_problems,
    evidence_rows,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

FILLED = {
    "Installs by anyone other than the author": "none known",
    "Issues or pull requests from anyone else": "0",
    "Demos shown, to whom, when": "none recorded",
    "User conversations written up": "none",
    "Experiment results published": "none",
}


def _table(rows: dict[str, str]) -> str:
    body = "".join(f"| {label} | {value} |\n" for label, value in rows.items())
    return f"| Field | Since 2026-08-19 |\n|-------|------------------|\n{body}"


def _amendment(section: str, date: str = "2026-10-01") -> str:
    """One amendment laid out as docs/v0.3.x-sequencing.md lays them out."""
    return (
        f"## Amendment {date} — open the next thing\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        f"{section}"
        "\n"
        "### Why this changes\n"
        "\n"
        "1. Because.\n"
    )


def _evidence(rows: dict[str, str]) -> str:
    return f"### External evidence since the last amendment\n\nMeasured today.\n\n{_table(rows)}"


def _problems(text: str) -> list[str]:
    (only,) = amendments(text)
    return evidence_problems(only)


def test_a_filled_section_passes_even_when_every_row_says_none() -> None:
    """An empty result is still evidence; rule 6 is what reads it."""
    assert _problems(_amendment(_evidence(FILLED))) == []


def test_an_amendment_without_the_section_fails() -> None:
    assert _problems(_amendment("")) == ["no External evidence since the last amendment section"]


def test_the_template_copied_without_filling_it_in_fails_on_every_field() -> None:
    blank = dict.fromkeys(FILLED, "")
    assert _problems(_amendment(_evidence(blank))) == [
        f"{field!r} is blank" for field in REQUIRED_FIELDS
    ]


@pytest.mark.parametrize("value", ["<fill in>", "…", "...", "  "])
def test_a_placeholder_counts_as_blank(value: str) -> None:
    rows = {**FILLED, "Demos shown, to whom, when": value}
    assert _problems(_amendment(_evidence(rows))) == ["'Demos shown, to whom, when' is blank"]


def test_a_missing_field_row_fails() -> None:
    rows = {k: v for k, v in FILLED.items() if k != "User conversations written up"}
    assert _problems(_amendment(_evidence(rows))) == ["no row for 'User conversations written up'"]


def test_a_section_with_no_table_fails_on_every_field() -> None:
    section = "### External evidence since the last amendment\n\nNothing happened.\n"
    assert _problems(_amendment(section)) == [f"no row for {field!r}" for field in REQUIRED_FIELDS]


def test_extra_rows_are_allowed_and_labels_ignore_case_and_bold() -> None:
    rows = {f"**{k.upper()}**": v for k, v in FILLED.items()}
    rows["Outside signals"] = "4 stars, 0 forks"
    assert _problems(_amendment(_evidence(rows))) == []


def test_rows_below_the_next_heading_do_not_fill_the_section() -> None:
    section = (
        "### External evidence since the last amendment\n\nSee below.\n\n"
        f"### Updated decision\n\n{_table(FILLED)}"
    )
    assert _problems(_amendment(section)) == [f"no row for {field!r}" for field in REQUIRED_FIELDS]


def test_a_hash_line_inside_a_code_fence_is_not_a_heading() -> None:
    """Dependency-chain and shell blocks sit inside amendments; ``# comment`` must not end one."""
    fence = "```bash\n# regenerate\n## Amendment 2099-01-01 — not real\nmake rfcs\n```\n\n"
    text = _amendment(fence + _evidence(FILLED))
    assert [a.date.isoformat() for a in amendments(text)] == ["2026-10-01"]
    assert _problems(text) == []


def test_amendments_are_split_at_each_level_two_heading() -> None:
    text = (
        "# Sequencing\n\n## Original decision\n\n"
        + _amendment(_evidence(FILLED), "2026-09-12")
        + "\n---\n\n"
        + _amendment("", "2026-11-30")
        + "\n## Related documentation\n\n"
        + _evidence({})
    )
    found = amendments(text)
    assert [(a.date.isoformat(), evidence_problems(a)) for a in found] == [
        ("2026-09-12", []),
        ("2026-11-30", ["no External evidence since the last amendment section"]),
    ]
    assert found[0].line == 5


def test_evidence_rows_reads_the_section_of_any_document() -> None:
    assert evidence_rows("# Doc\n\n" + _evidence(FILLED).replace("###", "##")) == {
        k.lower(): v for k, v in FILLED.items()
    }
    assert evidence_rows("# Doc\n\nNo section.\n") is None


def test_the_required_fields_are_the_amendment_templates_rows() -> None:
    """The template is what an author copies; the check must ask for the same rows."""
    template = REPO_ROOT / "docs" / "templates" / "PLAN_AMENDMENT_TEMPLATE.md"
    rows = evidence_rows(template.read_text(encoding="utf-8"))
    assert rows is not None
    assert list(rows) == [field.lower() for field in REQUIRED_FIELDS]


def test_this_checkout_passes() -> None:
    """The Amendment 2026-09-12 carries the first section; the check must accept it."""
    assert amendment_evidence.main([]) == 0


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty docs/ directory the check reads instead of the real one."""
    monkeypatch.setattr(amendment_evidence, "DOCS_DIR", tmp_path)
    return tmp_path


def test_the_check_fails_naming_the_file_line_and_problem(
    docs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """This is what the pre-commit hook and CI's Docs hygiene job run."""
    (docs / "v0.3.x-sequencing.md").write_text(
        "# Seq\n\n" + _amendment(_evidence(FILLED), "2026-09-12"), encoding="utf-8"
    )
    (docs / "v0.4.x-sequencing.md").write_text(
        "# Seq\n\n" + _amendment("", "2026-12-01"), encoding="utf-8"
    )
    assert amendment_evidence.main([]) == 1
    out = capsys.readouterr().out
    assert "[SCAN] Checked 2 amendment(s) dated 2026-09-12 or later in 2 sequencing doc(s)" in out
    assert "v0.4.x-sequencing.md:3: Amendment 2026-12-01 —" in out
    assert "no External evidence since the last amendment section" in out
    assert "v0.3.x-sequencing.md" not in out.split("[FAIL]", 1)[1]


def test_the_check_passes_once_the_section_is_filled(docs: Path) -> None:
    (docs / "v0.4.x-sequencing.md").write_text(
        "# Seq\n\n" + _amendment(_evidence(FILLED), "2026-12-01"), encoding="utf-8"
    )
    assert amendment_evidence.main([]) == 0


def test_amendments_older_than_the_rule_are_not_judged(docs: Path) -> None:
    """Rule 1 keeps earlier amendments verbatim, so they never had the section."""
    (docs / "v0.3.x-sequencing.md").write_text(
        "# Seq\n\n" + _amendment("", "2026-08-19") + _amendment(_evidence(FILLED), "2026-09-12"),
        encoding="utf-8",
    )
    assert amendment_evidence.main([]) == 0


def test_reading_no_amendment_under_the_rule_fails_instead_of_passing_empty(
    docs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A moved or renamed sequencing doc would otherwise leave a check that reads nothing."""
    (docs / "v0.3.x-sequencing.md").write_text(
        "# Seq\n\n" + _amendment("", "2026-08-19"), encoding="utf-8"
    )
    assert amendment_evidence.main([]) == 1
    assert "no amendment dated 2026-09-12 or later" in capsys.readouterr().out
