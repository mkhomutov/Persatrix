"""Guard against leaked agent / tool-call markup in tracked markdown.

Origin: PR #654 shipped ``docs/manual-tests/MT-CHANNEL-CONFIG-002.md`` with
trailing ``</content>`` / ``</invoke>`` lines — leftover Claude tool-call
markup that none of the existing doc gates catch. ``doc_links.py`` validates
only the *path* half of a markdown link (never free text), ``doc_status_markers``
only looks at status emoji, and ``file_size`` only counts bytes. So a raw block
of tool-invocation XML can ride into the repo unnoticed.

These tests pin the detector contract:

    * unambiguous tool-call tokens (``</invoke>``, ``<parameter name=``,
      the ``antml:`` namespace, …) are flagged anywhere on a line;
    * a bare ``</content>`` / ``<content>`` line (a partial Write/content
      leak) is flagged;
    * a legitimate ``<content>`` metavariable inside backticks
      (``<timestamp>  <sender>: <content>``) is NOT flagged;
    * ordinary prose and HTML (``<details>``, ``<code>``) are NOT flagged;
    * an explicit ``<!-- tool-markup-example -->`` opt-out suppresses a line
      (so a doc that legitimately documents tool-call syntax can keep it);
    * the live tracked-markdown corpus is clean.
"""

from __future__ import annotations

from pathlib import Path

from scripts.checks.doc_leaked_markup import check_leaked_markup, scan_text

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_flags_closing_invoke_tag() -> None:
    issues = scan_text("x.md", "intro line\n</invoke>\n")
    assert issues, "a bare </invoke> line must be flagged"
    assert issues[0].line == 2


def test_flags_standalone_content_tags() -> None:
    assert scan_text("x.md", "</content>\n"), "bare </content> must be flagged"
    assert scan_text("x.md", "<content>\n"), "bare <content> must be flagged"


def test_flags_antml_namespace_and_parameter() -> None:
    assert scan_text("x.md", '<parameter name="foo">bar</parameter>')
    assert scan_text("x.md", "leftover antml:invoke fragment")
    assert scan_text("x.md", "<function_calls>")


def test_ignores_content_placeholder_in_backticks() -> None:
    # Real doc metavariable — must not trip (MT-CHANNEL-002, RFC 0011 use it).
    text = "- human rows `<timestamp>  <sender>: <content>`, newest-first\n"
    assert scan_text("x.md", text) == []


def test_ignores_ordinary_prose_and_html() -> None:
    text = "# Title\n\nSome <code>inline</code> inside a <details> block.\n"
    assert scan_text("x.md", text) == []


def test_allow_comment_suppresses_a_documented_example() -> None:
    text = "`</invoke>` closes a call. <!-- tool-markup-example -->\n"
    assert scan_text("x.md", text) == []


def test_live_repo_markdown_is_clean() -> None:
    issues = check_leaked_markup(REPO_ROOT)
    rendered = "\n".join(f"  {i.file}:{i.line}  {i.token!r}" for i in issues)
    assert not issues, f"leaked tool-call markup found:\n{rendered}"
