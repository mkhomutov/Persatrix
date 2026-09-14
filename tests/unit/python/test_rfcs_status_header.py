"""Pin ``scripts/rfcs.py``'s status-header check.

Every RFC records its status twice: the ``status:`` front-matter field, which
``make rfcs`` renders into docs/rfcs/INDEX.md, and the bold ``**Status**:``
line GitHub shows under the title. Nothing compared the two, so RFC 0048's
header still said "🚧 Implementing" three months after #504 moved its
front-matter to ``partially_implemented``. The first ``**Status**:`` line
below the front-matter must now start with the INDEX marker for that status;
a qualifier may follow it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import rfcs

# ⚠️ is two code points: U+26A0 WARNING SIGN and U+FE0F VARIATION SELECTOR-16.
# Escaped so the selector is visible — the bare U+26A0 renders almost the same.
PARTIAL = "\u26a0\ufe0f Partially Implemented"
NAME = "0001-sample-rfc.md"


def _header(status_line: str) -> str:
    """The bold header block under an RFC's title, with ``status_line`` in it."""
    return f"# RFC 0001 — Sample\n\n**Type**: feature  \n{status_line}\n**Author**: Test  \n"


def _write_rfc(directory: Path, status: str | None, body: str) -> None:
    status_field = "" if status is None else f"status: {status}\n"
    (directory / NAME).write_text(
        "---\n"
        "id: RFC-0001\n"
        "title: Sample\n"
        "summary: A sample RFC.\n"
        "type: feature\n"
        f"{status_field}"
        "author: Test\n"
        "created: 2026-09-11\n"
        "target: v0.1\n"
        "---\n"
        "\n"
        f"{body}",
        encoding="utf-8",
    )


def _errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str | None, body: str
) -> list[str]:
    monkeypatch.setattr(rfcs, "RFCS_DIR", tmp_path)
    _write_rfc(tmp_path, status, body)
    collected, errors = rfcs.collect_rfcs()
    return errors + rfcs.validate(collected)


@pytest.mark.parametrize(
    "status, header",
    [
        ("implemented", "✅ Implemented"),
        ("implemented", "✅ Implemented  "),  # Markdown's two-space line break
        ("partially_implemented", f"{PARTIAL} (Phases 1–4)"),
        ("partially_implemented", f"{PARTIAL} — **Phases 1–2 shipped in v0.3.12** (PRs #779 …)"),
        ("implemented", "✅ **Implemented** — v0.3.12, all three phases"),  # bold label
        ("implemented", "✅ Implemented (partially superseded by the RFC 0011 amendment)"),
        ("implementing", "🚧 Implementing — Layer 2.5 shipped v0.3.6"),
        ("draft", "🔨 Draft (stub)"),
    ],
)
def test_a_header_that_opens_with_the_status_marker_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, header: str
) -> None:
    assert _errors(tmp_path, monkeypatch, status, _header(f"**Status**: {header}")) == []


@pytest.mark.parametrize(
    "status, header, expected",
    [
        pytest.param("partially_implemented", "🚧 Implementing", PARTIAL, id="rfc-0048"),
        pytest.param(
            "implemented",
            f"{PARTIAL} (Phases 1–4)",
            "✅ Implemented",
            id="partially-implemented-is-not-implemented",
        ),
        pytest.param("partially_implemented", "✅ Implemented", PARTIAL, id="nor-the-reverse"),
        pytest.param("implemented", "Implemented", "✅ Implemented", id="no-emoji"),
        pytest.param("draft", "🔨 Drafting", "🔨 Draft", id="label-ends-at-a-word-boundary"),
    ],
)
def test_a_header_that_disagrees_with_the_front_matter_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, header: str, expected: str
) -> None:
    errors = _errors(tmp_path, monkeypatch, status, _header(f"**Status**: {header}"))
    assert len(errors) == 1
    assert errors[0].startswith(f"{NAME}: ")
    assert f"'{status}'" in errors[0]
    assert f"'{expected}'" in errors[0]


def test_a_warning_sign_without_its_variation_selector_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = _header("**Status**: \u26a0 Partially Implemented (Phases 1–4)")
    errors = _errors(tmp_path, monkeypatch, "partially_implemented", header)
    assert len(errors) == 1
    assert "U+FE0F" in errors[0]  # the two render alike, so the message says why


@pytest.mark.parametrize(
    "status_line",
    [
        pytest.param("", id="no-status-line"),
        pytest.param("**Status:** 📋 Proposed", id="colon-inside-the-bold"),
    ],
)
def test_an_rfc_without_a_status_header_line_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_line: str
) -> None:
    errors = _errors(tmp_path, monkeypatch, "proposed", _header(status_line))
    assert len(errors) == 1
    assert "missing '**Status**:' header line" in errors[0]
    assert "'📋 Proposed'" in errors[0]


def test_only_the_first_status_line_below_the_front_matter_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    later = "\n## Phase 1\n\n**Status**: {}\n"
    agrees_first = _header("**Status**: 📋 Proposed") + later.format("✅ Implemented")
    assert _errors(tmp_path, monkeypatch, "proposed", agrees_first) == []
    disagrees_first = _header("**Status**: ✅ Implemented") + later.format("📋 Proposed")
    assert len(_errors(tmp_path, monkeypatch, "proposed", disagrees_first)) == 1


def test_no_header_complaint_without_a_valid_front_matter_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _header("**Status**: 📋 Proposed")
    assert _errors(tmp_path, monkeypatch, None, body) == []
    # An unknown status is reported once, as invalid — not again as a mismatch.
    errors = _errors(tmp_path, monkeypatch, "in_review", body)
    assert len(errors) == 1
    assert "invalid status" in errors[0]


def test_rfcs_check_fails_when_a_header_disagrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``rfcs.py --check`` is what the pre-commit hook and CI's Validate configs job run."""
    for name, value in (("RFCS_DIR", tmp_path), ("INDEX_FILE", tmp_path / "INDEX.md"),
                        ("REPO_ROOT", tmp_path)):
        monkeypatch.setattr(rfcs, name, value)
    monkeypatch.setattr(sys, "argv", ["rfcs.py", "--check"])
    _write_rfc(tmp_path, "partially_implemented", _header("**Status**: 🚧 Implementing"))
    # A fresh INDEX, so the header is the only thing that can fail the check.
    rfcs.INDEX_FILE.write_text(rfcs.render_index(rfcs.collect_rfcs()[0]), encoding="utf-8")
    assert rfcs.main() == 1
    assert NAME in capsys.readouterr().err
