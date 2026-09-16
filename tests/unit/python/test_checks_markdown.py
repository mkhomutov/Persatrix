"""Pin ``scripts/checks/markdown.py`` — the Markdown reading the doc checks share.

Three doc checks read Markdown structure: doc links (heading anchors), plan
status and ROADMAP status (table rows). Each used to parse it its own way, and
they disagreed with the page GitHub renders: one closed a code fence on any
line starting with three backticks, so a ```` block that shows a ``` line hid
the headings after it.
These tests pin the one reading they now share.
"""

from __future__ import annotations

import pytest

from scripts.checks import markdown


def _shown(text: str) -> list[str]:
    """The lines of *text* the page shows, blank where it hides one."""
    return markdown.rendered(text.split("\n"))


# --------------------------------------------------------------------------
# rendered — code fences and HTML comments are hidden, line numbers kept
# --------------------------------------------------------------------------

def test_lines_inside_a_code_fence_are_blanked_and_every_index_is_kept() -> None:
    text = "before\n```bash\n# a comment\n```\nafter"
    assert _shown(text) == ["before", "", "", "", "after"]


def test_a_four_backtick_fence_stays_open_past_a_three_backtick_line() -> None:
    """A ```` block may show a ``` block; only a bare ```` line closes it."""
    text = "````md\n```json\n{}\n```\n````\n## After"
    assert _shown(text) == ["", "", "", "", "", "## After"]


@pytest.mark.parametrize(
    ("closer", "why"),
    [
        pytest.param("~~~", "a tilde line cannot close a backtick fence", id="other-character"),
        pytest.param("``", "two backticks are not a fence", id="too-short"),
        pytest.param("```bash", "a closing fence carries no info string", id="info-string"),
    ],
)
def test_a_line_that_is_not_a_matching_bare_fence_does_not_close_one(closer: str, why: str) -> None:
    text = f"```\n{closer}\n# still code\n```\nshown"
    assert _shown(text) == ["", "", "", "", "shown"], why


def test_a_longer_bare_fence_of_the_same_character_closes_it() -> None:
    assert _shown("~~~\ncode\n~~~~~\nshown") == ["", "", "", "shown"]


def test_an_indented_fence_in_a_list_item_opens_and_closes() -> None:
    assert _shown("- step:\n  ```\n  make rfcs\n  ```\n- next") == ["- step:", "", "", "", "- next"]


def test_an_unclosed_fence_hides_the_rest_of_the_page() -> None:
    assert _shown("shown\n```\n# code\n## more code") == ["shown", "", "", ""]


def test_three_backticks_with_a_backtick_after_them_are_inline_code_not_a_fence() -> None:
    """CommonMark: a backtick fence's info string cannot hold a backtick."""
    text = "```py``` is inline code\n## Still A Heading"
    assert _shown(text) == text.split("\n")
    assert _shown("~~~ `odd` info\n# code\n~~~\nshown") == ["", "", "", "shown"]


def test_an_html_comment_is_hidden_on_every_line_it_spans() -> None:
    text = "shown\n<!-- a note\n## not rendered\n-->\nshown again\n<!-- one line -->\nlast"
    assert _shown(text) == ["shown", "", "", "", "shown again", "", "last"]


def test_a_fence_inside_a_comment_and_a_comment_inside_a_fence_open_nothing() -> None:
    assert _shown("<!--\n```\n-->\nshown") == ["", "", "", "shown"]
    assert _shown("```\n<!--\n```\nshown") == ["", "", "", "shown"]


# --------------------------------------------------------------------------
# headings — ATX headings on the rendered page
# --------------------------------------------------------------------------

def test_headings_give_the_index_level_and_text_of_each_rendered_heading() -> None:
    lines = [
        "# Title",
        "",
        "```",
        "# not a heading",
        "```",
        "<!-- ## not rendered -->",
        "### Third level   ",
        "###### Six",
    ]
    assert list(markdown.headings(lines)) == [(0, 1, "Title"), (6, 3, "Third level"), (7, 6, "Six")]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("## Design Notes", (2, "Design Notes"), id="plain"),
        pytest.param("## Design Notes ##", (2, "Design Notes"), id="closing-sequence-dropped"),
        pytest.param("## C# ##", (2, "C#"), id="hash-inside-text-kept"),
        pytest.param("## issue#12", (2, "issue#12"), id="no-space-before-hash-is-text"),
        pytest.param("#hashtag", None, id="no-space-after-hashes"),
        pytest.param("####### seven", None, id="seven-hashes"),
        pytest.param("text # not at the start", None, id="not-at-line-start"),
    ],
)
def test_heading_reads_one_line(line: str, expected: tuple[int, str] | None) -> None:
    assert markdown.heading(line) == expected


# --------------------------------------------------------------------------
# cells and is_divider — table rows
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("| a | b |", ["a", "b"], id="both-pipes"),
        pytest.param("| a | b", ["a", "b"], id="closing-pipe-optional"),
        pytest.param("  | a |  ", ["a"], id="surrounding-space"),
        pytest.param("| a || c |", ["a", "", "c"], id="empty-cell"),
        pytest.param("a | b |", None, id="no-leading-pipe"),
        pytest.param("|", None, id="lone-pipe"),
        pytest.param("", None, id="blank"),
    ],
)
def test_cells_split_a_table_row(line: str, expected: list[str] | None) -> None:
    assert markdown.cells(line) == expected


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        pytest.param(["---", ":--", "--:", ":-:"], True, id="alignments"),
        pytest.param(["---", "x"], False, id="text-cell"),
        pytest.param(["---", ""], False, id="empty-cell"),
        pytest.param(["- -"], False, id="split-dashes"),
    ],
)
def test_is_divider_knows_the_line_under_a_header_row(cells: list[str], expected: bool) -> None:
    assert markdown.is_divider(cells) is expected
