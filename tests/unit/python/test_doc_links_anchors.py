"""Pin the ``#anchor`` validation contract of ``scripts.checks.doc_links``.

``doc_links`` originally checked only that a link's *file* target existed;
a dangling ``file.md#does-not-exist`` (or in-page ``#does-not-exist``)
sailed through. These tests pin the behaviour added when the checker
learned to validate ``#anchor`` fragments against the target document's
actual headings using GitHub's slug algorithm (``github-slugger``):

    * the slug algorithm itself — lowercase, punctuation-stripped,
      spaces→hyphens, with **no** hyphen collapsing and **no** trimming;
    * heading extraction with duplicate ``-1``/``-2`` disambiguation,
      fenced-code-block skipping, inline-markdown rendering, and explicit
      ``<a id=…>`` anchors;
    * end-to-end link checking for valid cross-file / in-page anchors,
      broken anchors (which must fail), and the two deliberate exemptions
      — GitHub line anchors (``#L42``) and ``#fragment`` on non-markdown
      targets.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.checks.doc_links import (
    _extract_anchors,
    _github_slug,
    check_doc_links,
)

# --------------------------------------------------------------------------
# _github_slug — the GitHub (github-slugger) slug algorithm
# --------------------------------------------------------------------------

def test_slug_lowercases_and_hyphenates_spaces() -> None:
    assert _github_slug("Open Questions") == "open-questions"


def test_slug_strips_punctuation_but_keeps_underscores() -> None:
    # '/' and '()' are removed (not turned into hyphens) and '_' survives;
    # the spaces flanking the removed '/' become a doubled hyphen (no collapse).
    assert _github_slug("_infer_provider / resolve()") == "_infer_provider--resolve"


def test_slug_keeps_unicode_letters() -> None:
    # Accented letters are word characters — kept, not stripped.
    assert _github_slug("A. Memory Façade") == "a-memory-façade"


def test_slug_does_not_collapse_or_trim_hyphens() -> None:
    # A stripped leading emoji leaves a leading hyphen (no trim); an em-dash
    # separator leaves a doubled hyphen (no collapse). Both match GitHub —
    # e.g. README's "## ⚠️ Cost Warning" resolves to "#-cost-warning".
    assert _github_slug("⚠️ Cost Warning") == "-cost-warning"
    assert _github_slug("Scope — v0.4.0") == "scope--v040"


# --------------------------------------------------------------------------
# _extract_anchors — heading -> anchor-set extraction
# --------------------------------------------------------------------------

def test_extract_anchors_disambiguates_duplicate_headings() -> None:
    """Repeated headings get github-slugger's ``-1``/``-2`` suffixes."""
    content = "## Notes\n\na\n\n## Notes\n\nb\n\n## Notes\n"
    assert _extract_anchors(content) == {"notes", "notes-1", "notes-2"}


def test_extract_anchors_skips_fenced_code_blocks() -> None:
    """A ``#`` line inside a fenced code block is not a heading."""
    content = "# Real Heading\n\n```py\n# not a heading\n```\n\n## Also Real\n"
    assert _extract_anchors(content) == {"real-heading", "also-real"}


def test_extract_anchors_renders_inline_markdown() -> None:
    """Code-span backticks drop; links collapse to their text."""
    anchors = _extract_anchors("# The `foo` [bar](https://example.com) baz\n")
    assert anchors == {"the-foo-bar-baz"}


def test_extract_anchors_collects_explicit_html_anchors() -> None:
    """``<a id=…>`` / ``<a name=…>`` are anchor targets GitHub honours."""
    content = '# Heading\n\n<a id="decided-x"></a>\n\n<a name="legacy"></a>\n'
    assert _extract_anchors(content) == {"heading", "decided-x", "legacy"}


# --------------------------------------------------------------------------
# check_doc_links — end-to-end anchor validation
# --------------------------------------------------------------------------

def test_valid_cross_file_anchor_passes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "target.md").write_text(
        "# Intro\n\n## Design Notes\n", encoding="utf-8",
    )
    (tmp_path / "src.md").write_text(
        "See the [notes](target.md#design-notes).\n", encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    assert check_doc_links(tmp_path) == []


def test_valid_in_page_anchor_passes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "doc.md").write_text(
        "# Title\n\n## Section One\n\nBack to [top](#title) / [s1](#section-one).\n",
        encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    assert check_doc_links(tmp_path) == []


def test_broken_cross_file_anchor_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "target.md").write_text(
        "# Intro\n\n## Design Notes\n", encoding="utf-8",
    )
    (tmp_path / "src.md").write_text(
        "See the [notes](target.md#does-not-exist).\n", encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    failures = check_doc_links(tmp_path)
    assert len(failures) == 1
    assert failures[0].file == "src.md"
    assert "does-not-exist" in failures[0].reason


def test_broken_in_page_anchor_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "doc.md").write_text(
        "# Title\n\nJump to [nowhere](#nowhere).\n", encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    failures = check_doc_links(tmp_path)
    assert len(failures) == 1
    assert "nowhere" in failures[0].reason


def test_duplicate_heading_suffix_resolves_end_to_end(tmp_path: Path) -> None:
    """A link to the ``-1`` disambiguation slug resolves; ``-2`` does not."""
    _init_git_repo(tmp_path)
    (tmp_path / "target.md").write_text(
        "## Scope\n\na\n\n## Scope\n", encoding="utf-8",
    )
    (tmp_path / "src.md").write_text(
        "First [scope](target.md#scope), second [scope](target.md#scope-1), "
        "missing [scope](target.md#scope-2).\n",
        encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    failures = check_doc_links(tmp_path)
    assert len(failures) == 1
    assert "scope-2" in failures[0].reason


def test_explicit_html_anchor_resolves(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "doc.md").write_text(
        '# Plan\n\n<a id="out-of-scope"></a>**Out of scope** — see '
        "[here](#out-of-scope).\n",
        encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    assert check_doc_links(tmp_path) == []


def test_github_line_anchor_on_markdown_is_exempt(tmp_path: Path) -> None:
    """``#L42`` is a GitHub line anchor, not a heading — never validated."""
    _init_git_repo(tmp_path)
    (tmp_path / "target.md").write_text("# Only Heading\n", encoding="utf-8")
    (tmp_path / "src.md").write_text(
        "See [line 42](target.md#L42) and [range](target.md#L42-L60).\n",
        encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    assert check_doc_links(tmp_path) == []


def test_anchor_on_non_markdown_target_is_exempt(tmp_path: Path) -> None:
    """A ``#fragment`` on a non-markdown file is not a heading slug."""
    _init_git_repo(tmp_path)
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "doc.md").write_text(
        "See [code](code.py#some-symbol) and [line](code.py#L1).\n",
        encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    assert check_doc_links(tmp_path) == []


# --------------------------------------------------------------------------
# git fixtures (mirrors test_doc_links_collection.py)
# --------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _init_git_repo(path: Path) -> None:
    env = _git_env()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, env=env)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path, check=True, env=env,
    )


def _git_add_commit(path: Path, target: str, message: str) -> None:
    env = _git_env()
    subprocess.run(["git", "add", target], cwd=path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=path, check=True, env=env,
    )


@pytest.fixture(autouse=True)
def _ensure_git_available() -> None:
    """Skip cleanly if ``git`` is not on PATH (mirrors the sibling suite)."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):  # pragma: no cover
        pytest.skip("git not available on PATH")
