"""Pin what ``scripts/checks/amendment_evidence.py`` reads and reports.

The page-level reading — sections, rows, values, headings — is pinned in
``test_amendment_evidence.py``. These run the whole check the way the
pre-commit hook and CI's Docs hygiene job do: which files under ``docs/``
count as the sequencing record, which headings fail, and what it prints.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _amendment_evidence_helpers import FILLED, _amendment, _evidence
from _test_infra import ci_job_steps, makefile_recipe_body

from scripts.checks import amendment_evidence


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


def _with_first_amendment(docs: Path) -> None:
    """The filled 2026-09-12 amendment every checkout carries from now on."""
    (docs / "v0.3.x-sequencing.md").write_text(
        "# Seq\n\n" + _amendment(_evidence(FILLED), "2026-09-12"), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "heading",
    [
        "## Amendment — 2026-11-30 — open the next thing",
        "## AMENDMENT 2026-11-30 — open the next thing",
        "### Amendment 2026-11-30 — open the next thing",
        "## 2026-11-30 amendment — open the next thing",
        "# Amendment 2026-11-30 — open the next thing",
        "# v0.4.x sequencing amendment — 2026-11-30 — open the next thing",
        "## Sequencing amendment 2026-11-30 — open the next thing",
        "## Amendments 2026-11-30 — open the next thing",
        "## Amendment #3 (2026-11-30) — open the next thing",
        "## Amendment of 2026-11-30 — open the next thing",
        "## Amendment 2026‑11‑30 — open the next thing",
        "## `Amendment 2026-11-30` — open the next thing",
        "## [Amendment 2026-11-30](#x) — open the next thing",
    ],
)
def test_an_amendment_heading_in_another_form_fails_instead_of_being_skipped(
    docs: Path, capsys: pytest.CaptureFixture[str], heading: str
) -> None:
    """The 2026-09-12 amendment always counts, so a skipped new one would pass unseen."""
    _with_first_amendment(docs)
    (docs / "v0.4.x-sequencing.md").write_text(f"# Seq\n\n{heading}\n\nIntro.\n", encoding="utf-8")
    assert amendment_evidence.main([]) == 1
    out = capsys.readouterr().out
    assert f"v0.4.x-sequencing.md:3: {heading.lstrip('#').strip()} — " in out
    assert "not in the `## Amendment YYYY-MM-DD — <title>` form" in out


def test_a_setext_amendment_heading_fails_instead_of_being_skipped(
    docs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub shows a line over ``---`` as a level-2 heading."""
    _with_first_amendment(docs)
    (docs / "v0.4.x-sequencing.md").write_text(
        "# Seq\n\nAmendment 2026-11-30 — open the next thing\n---\n\nIntro.\n", encoding="utf-8"
    )
    assert amendment_evidence.main([]) == 1
    out = capsys.readouterr().out
    assert "v0.4.x-sequencing.md:3: Amendment 2026-11-30 — open the next thing — not in the" in out


@pytest.mark.parametrize("named", ["2026-09-12", "2026-11-30"])
def test_a_sub_heading_naming_an_amendment_no_newer_than_its_own_is_a_reference(
    docs: Path, named: str
) -> None:
    (docs / "v0.3.x-sequencing.md").write_text(
        "# Seq\n\n"
        + _amendment(_evidence(FILLED), "2026-09-12")
        + _amendment(
            _evidence(FILLED) + f"\n### Amendment {named} revisited\n\nText.\n", "2026-11-30"
        ),
        encoding="utf-8",
    )
    assert amendment_evidence.main([]) == 0


def test_a_sub_heading_naming_a_newer_amendment_still_fails(
    docs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under the 2026-09-12 amendment, a later ``###`` is a new amendment at the wrong level."""
    (docs / "v0.3.x-sequencing.md").write_text(
        "# Seq\n\n"
        + _amendment(
            _evidence(FILLED) + "\n### Amendment 2026-11-30 — open\n\nText.\n", "2026-09-12"
        ),
        encoding="utf-8",
    )
    assert amendment_evidence.main([]) == 1
    assert "Amendment 2026-11-30 — open — not in the" in capsys.readouterr().out


def test_an_older_heading_in_another_form_is_left_alone(docs: Path) -> None:
    _with_first_amendment(docs)
    (docs / "v0.2.x-sequencing.md").write_text(
        "# Seq\n\n### Amendment 2026-05-01 — old\n\nKept verbatim.\n", encoding="utf-8"
    )
    assert amendment_evidence.main([]) == 0


def test_an_impossible_date_is_reported_not_raised(
    docs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_first_amendment(docs)
    (docs / "v0.4.x-sequencing.md").write_text(
        "# Seq\n\n" + _amendment(_evidence(FILLED), "2026-11-31"), encoding="utf-8"
    )
    assert amendment_evidence.main([]) == 1
    out = capsys.readouterr().out
    assert "v0.4.x-sequencing.md:3: Amendment 2026-11-31 — open the next thing —" in out
    assert "2026-11-31 is not a date" in out


@pytest.mark.parametrize(
    "rel", ["sequencing/v0.4.x-sequencing.md", "v0.4.x-sequencing-amendment-2026-12-01.md"]
)
def test_a_sequencing_doc_in_a_subdirectory_or_its_own_file_is_read(
    docs: Path, capsys: pytest.CaptureFixture[str], rel: str
) -> None:
    _with_first_amendment(docs)
    (docs / rel).parent.mkdir(exist_ok=True)
    (docs / rel).write_text("# Seq\n\n" + _amendment("", "2026-12-01"), encoding="utf-8")
    assert amendment_evidence.main([]) == 1
    assert f"{rel}:3: Amendment 2026-12-01 —" in capsys.readouterr().out


@pytest.mark.parametrize(
    "rel", ["rfcs/0099-release-sequencing.md", "issues/ISSUE-9999-sequencing-example.md"]
)
def test_a_doc_that_only_mentions_sequencing_is_not_the_record(docs: Path, rel: str) -> None:
    (docs / rel).parent.mkdir()
    (docs / rel).write_text("# Example\n\n" + _amendment(_evidence(FILLED), "2026-12-01"))
    assert amendment_evidence.main([]) == 1  # it neither counts as the record…
    _with_first_amendment(docs)
    (docs / rel).write_text("# Example\n\n### Amendment 2026-12-01 — example\n")
    assert amendment_evidence.main([]) == 0  # …nor is judged


def test_a_local_only_review_report_is_not_read(docs: Path) -> None:
    """docs/pr-reviews/ is gitignored; a quoted amendment there is not the record."""
    _with_first_amendment(docs)
    (docs / "pr-reviews").mkdir()
    (docs / "pr-reviews" / "v0.4.x-sequencing.md").write_text(
        "# Review\n\n" + _amendment("", "2026-12-01"), encoding="utf-8"
    )
    assert amendment_evidence.main([]) == 0


#: The fixture repository ignores the caller's git setup.
_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def test_in_a_checkout_only_tracked_docs_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untracked draft is not on main; CI would not see it, so the hook must not either."""
    docs = tmp_path / "docs"
    docs.mkdir()
    _with_first_amendment(docs)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=_GIT_ENV)
    subprocess.run(["git", "add", "docs"], cwd=tmp_path, check=True, env=_GIT_ENV)
    (docs / "v0.4.x-sequencing.md").write_text("# Seq\n\n" + _amendment("", "2026-12-01"))
    monkeypatch.setattr(amendment_evidence, "DOCS_DIR", docs)
    assert amendment_evidence.main([]) == 0


@pytest.mark.parametrize(
    ("page", "cause"),
    [
        (
            "# Seq\n\n```bash\nx\n\n" + _amendment(_evidence(FILLED), "2026-09-12"),
            "v0.3.x-sequencing.md:3: a code fence or HTML comment opened here never closes",
        ),
        (
            "# Seq\n\n" + _amendment(_evidence(FILLED), "2026-09-31"),
            "2026-09-31 is not a date",
        ),
    ],
)
def test_reading_no_amendment_still_names_what_hid_it(
    docs: Path, capsys: pytest.CaptureFixture[str], page: str, cause: str
) -> None:
    (docs / "v0.3.x-sequencing.md").write_text(page, encoding="utf-8")
    assert amendment_evidence.main([]) == 1
    out = capsys.readouterr().out
    assert "no amendment dated 2026-09-12 or later" in out
    assert cause in out


def test_ci_and_make_run_the_check() -> None:
    """The manifest lists the check as Persatrix-specific, so conformance no longer pins these."""
    script = "scripts/checks/amendment_evidence.py"
    assert script in makefile_recipe_body("amendment-evidence-check")
    assert any(script in step.get("run", "") for step in ci_job_steps("docs-hygiene"))


def test_a_fence_that_never_closes_fails_instead_of_hiding_what_follows(
    docs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every line after an unclosed fence is code, so a new amendment there would go unread."""
    (docs / "v0.3.x-sequencing.md").write_text(
        "# Seq\n\n"
        + _amendment(_evidence(FILLED) + "\n````markdown\n```bash\n```\n", "2026-09-12")
        + _amendment("", "2026-11-30"),
        encoding="utf-8",
    )
    assert amendment_evidence.main([]) == 1
    assert (
        "v0.3.x-sequencing.md:19: a code fence or HTML comment opened here never closes"
        in capsys.readouterr().out
    )
