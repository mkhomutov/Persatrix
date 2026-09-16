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
from _amendment_evidence_helpers import FILLED, _amendment, _evidence, _table

from scripts.checks import amendment_evidence, markdown_page
from scripts.checks.amendment_evidence import (
    REQUIRED_FIELDS,
    TEMPLATE_SECTIONS,
    amendments,
    evidence_problems,
    evidence_rows,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


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


@pytest.mark.parametrize(
    "value",
    [
        "<fill in>",
        "…",
        "...",
        "  ",
        "TBD",
        "TODO: after the demo",
        "?",
        "-",
        "—",
        "**",
        "<count: fill in>",
        "<date: when>",
        "`<fill in>`",
        "`TBD`",
        "[TBD]",
        "pending",
        "TBD - after the demo",
        "TBD (after the demo)",
        "TBD.",
    ],
)
def test_a_placeholder_counts_as_blank(value: str) -> None:
    rows = {**FILLED, "Demos shown, to whom, when": value}
    assert _problems(_amendment(_evidence(rows))) == ["'Demos shown, to whom, when' is blank"]


@pytest.mark.parametrize(
    "value",
    [
        "Todo list app demo to Alice, 2026-11-20",
        "XXX Corp installed it",
        "pending review by Alice: none",
        "one \\| two",
    ],
)
def test_evidence_that_starts_like_a_placeholder_is_not_blank(value: str) -> None:
    """Only a bare marker defers a row; a sentence that opens with the same word is evidence."""
    rows = {**FILLED, "Demos shown, to whom, when": value}
    assert _problems(_amendment(_evidence(rows))) == []


@pytest.mark.parametrize(
    "value",
    [
        "<https://github.com/mkhomutov/Persatrix/issues/970>",
        "<mailto:someone@example.com>",
        '<a href="https://github.com/mkhomutov/Persatrix/issues/970">#970</a>',
        "[#970](https://github.com/mkhomutov/Persatrix/issues/970)",
    ],
)
def test_a_link_is_evidence_not_a_placeholder(value: str) -> None:
    """The first outside issue is the entry rule 6 exists for; it must not read as blank."""
    rows = {**FILLED, "Issues or pull requests from anyone else": value}
    assert _problems(_amendment(_evidence(rows))) == []


def test_a_row_without_a_closing_pipe_still_counts() -> None:
    """GitHub renders a row with no trailing ``|``; the row after it is not a header."""
    table = _table(FILLED).replace("| none known |", "| none known")
    section = f"### External evidence since the last amendment\n\n{table}"
    assert _problems(_amendment(section)) == []


def test_a_divider_without_outer_pipes_still_makes_a_table() -> None:
    """GitHub accepts ``---|---`` under a header row."""
    table = _table(FILLED).replace("|-------|------------------|", "-------|------------------")
    section = f"### External evidence since the last amendment\n\n{table}"
    assert _problems(_amendment(section)) == []


def test_pipe_lines_with_no_divider_under_the_header_are_not_a_table() -> None:
    """GitHub shows them as a paragraph, so they record nothing."""
    table = _table(FILLED).replace("|-------|------------------|\n", "")
    section = f"### External evidence since the last amendment\n\n{table}"
    assert _problems(_amendment(section)) == [f"no row for {field!r}" for field in REQUIRED_FIELDS]


def test_a_field_given_twice_fails() -> None:
    """Which of two rows counts would depend on their order, so neither is picked."""
    section = _evidence(FILLED) + "| Demos shown, to whom, when | |\n"
    assert _problems(_amendment(section)) == ["'Demos shown, to whom, when' has 2 rows; keep one"]


@pytest.mark.parametrize(("opening", "closing"), [("```markdown\n", "```\n"), ("<!--\n", "-->\n")])
def test_a_table_that_does_not_render_does_not_fill_the_section(opening: str, closing: str) -> None:
    section = (
        "### External evidence since the last amendment\n\nThe format is:\n\n"
        f"{opening}{_table(FILLED)}{closing}"
    )
    assert _problems(_amendment(section)) == [f"no row for {field!r}" for field in REQUIRED_FIELDS]


@pytest.mark.parametrize(
    "heading",
    [
        "## External evidence since the last amendment",
        "### **External evidence since the last amendment**",
    ],
)
def test_the_section_heading_may_keep_the_templates_level_or_be_bold(heading: str) -> None:
    """The template's heading is ``##``; pasted as it is, it still belongs to the amendment."""
    section = f"{heading}\n\n{_table(FILLED)}"
    assert _problems(_amendment(section)) == []


def test_the_template_pasted_as_it_is_keeps_its_sections_in_the_amendment() -> None:
    """The template writes Context, then the evidence, then the rest, all at ``##``."""
    text = (
        "# Seq\n\n## Amendment 2026-11-30 — open the next thing\n\n"
        "## Context\n\nWhat was learned.\n\n"
        f"## External evidence since the last amendment\n\n{_table(FILLED)}\n"
        "## What changes\n\n| # | Before | After |\n|---|---|---|\n| 1 | a | b |\n\n"
        "## Related documentation\n\n" + _evidence({})
    )
    assert [(a.date.isoformat(), evidence_problems(a)) for a in amendments(text)] == [
        ("2026-11-30", [])
    ]


def test_the_sections_kept_in_an_amendment_are_the_templates() -> None:
    """Every ``##`` the template writes but the last: Related documentation closes the page too."""
    template = REPO_ROOT / "docs" / "templates" / "PLAN_AMENDMENT_TEMPLATE.md"
    lines = template.read_text(encoding="utf-8").splitlines()
    titles = [t for _, level, t in markdown_page.headings(lines) if level == 2]
    assert titles[-1] == "Related documentation"
    assert tuple(titles[:-1]) == TEMPLATE_SECTIONS


def test_a_level_one_amendment_heading_is_not_read_as_an_amendment() -> None:
    """The one form read is ``##``; any other level is reported, not judged as well."""
    assert amendments("# Amendment 2026-11-30 — open the next thing\n\n" + _evidence(FILLED)) == []


def test_a_title_keeps_the_underscores_inside_its_words() -> None:
    text = _amendment(_evidence(FILLED)).replace(
        "open the next thing", "set MEMORY_BUDGET_TOKENS, *not* __this__"
    )
    assert [a.title for a in amendments(text)] == [
        "Amendment 2026-10-01 — set MEMORY_BUDGET_TOKENS, not this"
    ]


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


def test_a_fence_closes_only_on_its_own_marker_at_least_as_long() -> None:
    """A ```` block that shows a ``` line, or a ~~~ block that holds one, stays one block."""
    fences = (
        "````markdown\n```bash\n# regenerate\n````\n\n"
        "~~~\n```\n## Amendment 2099-01-01 — not real\n~~~\n\n"
    )
    text = (
        _amendment(fences + _evidence(FILLED), "2026-09-12")
        + "\n---\n\n"
        + _amendment("", "2026-11-30")
    )
    assert [(a.date.isoformat(), evidence_problems(a)) for a in amendments(text)] == [
        ("2026-09-12", []),
        ("2026-11-30", ["no External evidence since the last amendment section"]),
    ]


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
    doc = "# Doc\n\n" + _evidence(FILLED).replace("###", "##")
    assert evidence_rows(doc.splitlines()) == {k.lower(): [v] for k, v in FILLED.items()}
    assert evidence_rows("# Doc\n\nNo section.\n".splitlines()) is None


def test_the_required_fields_are_the_amendment_templates_rows() -> None:
    """The template is what an author copies; the check must ask for the same rows."""
    template = REPO_ROOT / "docs" / "templates" / "PLAN_AMENDMENT_TEMPLATE.md"
    rows = evidence_rows(template.read_text(encoding="utf-8").splitlines())
    assert rows is not None
    assert list(rows) == [field.lower() for field in REQUIRED_FIELDS]


def test_this_checkout_passes() -> None:
    """The Amendment 2026-09-12 carries the first section; the check must accept it."""
    assert amendment_evidence.main([]) == 0
