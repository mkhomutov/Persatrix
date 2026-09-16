"""Pin ``scripts/checks/markdown_page.py`` — the Markdown reading the doc checks share.

Three doc checks read Markdown structure: doc links (heading anchors), plan
status and ROADMAP status (table rows). Each used to parse it its own way, and
they disagreed with the page GitHub renders: one closed a code fence on any
line starting with three backticks, so a ```` block that shows a ``` line hid
the headings after it.
These tests pin the one reading they now share.
"""

from __future__ import annotations

import pytest

from scripts.checks import markdown_page


def _shown(text: str) -> list[str]:
    """The lines of *text* the page shows, blank where it hides one."""
    return markdown_page.rendered(text.split("\n"))


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


def test_a_fence_line_indented_four_spaces_inside_a_fence_is_code_not_a_closer() -> None:
    """A docstring in a Python example may show ``` lines; they do not end the example."""
    text = "```python\ndef f():\n    ```\n| 1 | 🔀 PR open |\n    ```\n```\nshown"
    assert _shown(text) == ["", "", "", "", "", "", "shown"]


def test_a_fence_line_indented_four_spaces_outside_a_list_opens_nothing() -> None:
    """Four spaces in make an indented code block, so the ``` is shown as code, not a fence."""
    assert _shown("text\n\n    ```\n## Heading") == ["text", "", "    ```", "## Heading"]


def test_a_list_that_has_ended_no_longer_lets_a_fence_sit_deeper() -> None:
    text = "- item\n\ntext\n    ```\n## Heading"
    assert _shown(text) == ["- item", "", "text", "    ```", "## Heading"]


def test_a_fence_indented_under_a_numbered_item_opens_and_closes() -> None:
    text = "1. Do:\n\n    ```bash\n    make\n    ```\nafter"
    assert _shown(text) == ["1. Do:", "", "", "", "", "after"]


def test_a_fence_in_a_list_item_ends_when_the_item_ends() -> None:
    """GitHub closes a list item's fence at the item's end, even with no closing line."""
    text = "- step:\n  ```\n  make\n- next\n\n## Heading"
    assert _shown(text) == ["- step:", "", "", "- next", "", "## Heading"]


def test_a_top_level_indented_fence_keeps_less_indented_code() -> None:
    assert _shown("   ```\ncode\n   ```\nshown") == ["", "", "", "shown"]


def test_an_unclosed_fence_hides_the_rest_of_the_page() -> None:
    assert _shown("shown\n```\n# code\n## more code") == ["shown", "", "", ""]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("shown\n```\n# code", 1, id="open-fence"),
        pytest.param("shown\n<!-- note\nmore", 1, id="open-comment"),
        pytest.param("```\ncode\n```\n<!-- a -->", None, id="all-closed"),
        pytest.param("- step:\n  ```\n- next", None, id="closed-by-the-list-item"),
        pytest.param("", None, id="empty"),
    ],
)
def test_unclosed_names_the_line_of_a_fence_or_comment_the_page_never_closes(
    text: str, expected: int | None
) -> None:
    assert markdown_page.unclosed(text.split("\n")) == expected


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
    expected = [(0, 1, "Title"), (6, 3, "Third level"), (7, 6, "Six")]
    assert list(markdown_page.headings(lines)) == expected


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
        pytest.param(" ## One space in", (2, "One space in"), id="indented-one-space"),
        pytest.param("   ### Three spaces in", (3, "Three spaces in"), id="indented-three-spaces"),
        pytest.param("    # four spaces in", None, id="indented-four-spaces-is-code"),
    ],
)
def test_heading_reads_one_line(line: str, expected: tuple[int, str] | None) -> None:
    assert markdown_page.heading(line) == expected


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
        pytest.param("a | b |", ["a", "b"], id="leading-pipe-optional"),
        pytest.param("a | b", None, id="no-outer-pipe"),
        pytest.param("| a \\| b | c |", ["a | b", "c"], id="escaped-pipe"),
        pytest.param("| a \\|", ["a |"], id="escaped-closing-pipe"),
        pytest.param("---|:--:", ["---", ":--:"], id="divider-without-outer-pipes"),
        pytest.param("|", None, id="lone-pipe"),
        pytest.param("", None, id="blank"),
    ],
)
def test_cells_split_a_table_row(line: str, expected: list[str] | None) -> None:
    assert markdown_page.cells(line) == expected


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        pytest.param(["---", ":--", "--:", ":-:"], True, id="alignments"),
        pytest.param(["---", "x"], False, id="text-cell"),
        pytest.param(["---", ""], False, id="empty-cell"),
        pytest.param(["- -"], False, id="split-dashes"),
        pytest.param([], False, id="no-cells"),
    ],
)
def test_is_divider_knows_the_line_under_a_header_row(cells: list[str], expected: bool) -> None:
    assert markdown_page.is_divider(cells) is expected
