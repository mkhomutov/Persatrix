"""Pin the contract of ``scripts.checks.ui_html_directive`` — the §A3 XSS gate.

The gate (RFC 0039 enabled-mode exposure amendment §A3) rejects Svelte's
``{@html …}`` directive under ``web/src`` because the console renders
LLM-authored content and the session is a cookie. Pinned here:

* The directive trips whether its expression follows on the same line or
  after a newline — Svelte accepts either, so the scan must run over the
  full file text, not per line (the per-line form was a review finding:
  ``{@html\\n  expr}`` slipped through).
* Prose *about* the directive (a bare ``{@html}`` in a comment — exactly
  what the two grandfathered mentions in ``web/src`` are) does not trip.
* Reported line numbers point at the directive, so a CI failure is
  clickable.
* Only source extensions are scanned, and a missing root is a clean
  no-finding pass, not an error.
"""

from __future__ import annotations

from pathlib import Path

from scripts.checks.ui_html_directive import find_html_directives


def _write(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.write_text(content, encoding="utf-8")
    return path


def test_single_line_directive_trips(tmp_path: Path) -> None:
    _write(tmp_path, "Panel.svelte", "<div>\n  {@html userContent}\n</div>\n")
    findings = find_html_directives(tmp_path)
    assert [(f.path.name, f.line_no) for f in findings] == [("Panel.svelte", 2)]


def test_multi_line_directive_trips(tmp_path: Path) -> None:
    """The review-finding regression: a newline after ``{@html`` is
    valid Svelte and must not evade the gate."""
    _write(tmp_path, "Panel.svelte", "<div>\n  {@html\n    userContent}\n</div>\n")
    findings = find_html_directives(tmp_path)
    assert [(f.path.name, f.line_no) for f in findings] == [("Panel.svelte", 2)]


def test_prose_mention_does_not_trip(tmp_path: Path) -> None:
    """A bare ``{@html}`` in a comment is prose about the directive —
    the form the two existing web/src mentions take."""
    _write(
        tmp_path,
        "mentions.js",
        "// Never use {@html} here: the content is LLM-authored.\n",
    )
    assert find_html_directives(tmp_path) == []


def test_non_source_extensions_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "notes.md", "Example: {@html expr} is forbidden.\n")
    assert find_html_directives(tmp_path) == []


def test_missing_root_is_clean(tmp_path: Path) -> None:
    assert find_html_directives(tmp_path / "does-not-exist") == []
