"""Pin the contract of ``scripts.checks.doc_links._collect_md_files``.

ISSUE-0036: the previous implementation collected markdown files via
``repo_root.glob('*.md')`` + ``repo_root.glob('*/*.md')`` + a
``docs/**/*.md`` recursive walk, then filtered out ``.git/`` and
``docs/`` matches that crept in. That shape silently dropped every
tracked markdown file at depth ≥ 3 outside ``docs/`` (e.g.
``.github/instructions/*.md``, ``prompts/runtime/safety/*.md``) and
double-relied on ad-hoc filters to keep stash artifacts under
``.git/`` out of the result.

These tests pin the post-fix contract:

    * A file tracked by git at depth ≥ 3 outside ``docs/`` is collected.
    * Untracked / git-internal markdown files are not collected.
    * ``docs/pr-reviews/`` stays excluded (project convention — see
      ``.github/copilot-instructions.md``).
    * Outside a git checkout, the function falls back to the legacy
      glob behaviour rather than crashing — preserves the script's
      defensive posture for downstream tarball consumers.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.checks.doc_links import _collect_md_files

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git_tracked_md_files(repo_root: Path) -> set[Path]:
    """Source-of-truth set: every tracked ``*.md`` resolved to absolute path."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return {
        (repo_root / Path(name.decode("utf-8"))).resolve()
        for name in result.stdout.split(b"\0")
        if name
    }


def test_collects_tracked_md_at_depth_three_outside_docs() -> None:
    """A tracked markdown file three levels deep outside ``docs/`` is collected.

    Pre-fix this fails because the glob walks only ``docs/**/*.md`` +
    ``repo_root/*.md`` + ``repo_root/*/*.md`` — depth-3 files like
    ``.github/instructions/python-agents.instructions.md`` are missed.
    """
    target = REPO_ROOT / ".github" / "instructions" / "python-agents.instructions.md"
    assert target.is_file(), (
        "fixture file went missing — update the test target to another "
        "tracked depth-3 markdown file outside docs/"
    )

    files = {p.resolve() for p in _collect_md_files(REPO_ROOT)}
    assert target.resolve() in files


def test_collects_tracked_md_at_depth_four_or_more() -> None:
    """A tracked markdown file four+ levels deep is collected."""
    target = REPO_ROOT / "prompts" / "runtime" / "safety" / "memory-preamble.md"
    assert target.is_file(), (
        "fixture file went missing — update the test target to another "
        "tracked depth-4+ markdown file"
    )

    files = {p.resolve() for p in _collect_md_files(REPO_ROOT)}
    assert target.resolve() in files


def test_matches_git_ls_files_minus_pr_reviews() -> None:
    """The collection equals ``git ls-files '*.md'`` minus ``docs/pr-reviews/``.

    This is the strongest formulation of the contract: the collector's
    source of truth is the git index, with one project-convention
    exclusion (PR review reports are local-only artifacts).
    """
    expected = _git_tracked_md_files(REPO_ROOT)
    pr_reviews = (REPO_ROOT / "docs" / "pr-reviews").resolve()
    expected = {
        f for f in expected
        if pr_reviews not in f.parents and f != pr_reviews
    }

    actual = {p.resolve() for p in _collect_md_files(REPO_ROOT)}

    missing = expected - actual
    extra = actual - expected
    assert not missing and not extra, (
        f"collected set diverges from `git ls-files '*.md'`. "
        f"missing={sorted(map(str, missing))[:5]} "
        f"extra={sorted(map(str, extra))[:5]}"
    )


def test_excludes_pr_reviews_directory(tmp_path: Path) -> None:
    """``docs/pr-reviews/*.md`` stays excluded — project convention.

    Materialises a minimal git checkout under ``tmp_path`` so the test
    does not depend on the live repo's pr-reviews state (which is
    gitignored and may be empty).
    """
    _init_git_repo(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text("# real\n", encoding="utf-8")
    (tmp_path / "docs" / "pr-reviews").mkdir()
    (tmp_path / "docs" / "pr-reviews" / "review.md").write_text(
        "# review\n", encoding="utf-8",
    )
    _git_add_commit(tmp_path, ".", "seed")

    files = {p.resolve() for p in _collect_md_files(tmp_path)}

    assert (tmp_path / "docs" / "real.md").resolve() in files
    assert (tmp_path / "docs" / "pr-reviews" / "review.md").resolve() not in files


def test_excludes_untracked_md(tmp_path: Path) -> None:
    """Markdown files in the working tree but not tracked by git are excluded.

    Pre-fix the glob picked up *any* file in the working tree that
    matched the pattern, including ``.git/PR_BODY.md`` left behind by
    ``git stash``. Post-fix the source of truth is the git index, so
    untracked artifacts are filtered out by construction.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "tracked.md").write_text("# tracked\n", encoding="utf-8")
    _git_add_commit(tmp_path, "tracked.md", "seed")

    untracked = tmp_path / "PR_BODY.md"
    untracked.write_text("# stash artifact\n", encoding="utf-8")

    files = {p.resolve() for p in _collect_md_files(tmp_path)}

    assert (tmp_path / "tracked.md").resolve() in files
    assert untracked.resolve() not in files


def test_excludes_git_internal_directory(tmp_path: Path) -> None:
    """Markdown files under ``.git/`` (e.g. stash residue) are excluded."""
    _init_git_repo(tmp_path)
    (tmp_path / "tracked.md").write_text("# tracked\n", encoding="utf-8")
    _git_add_commit(tmp_path, "tracked.md", "seed")

    git_internal = tmp_path / ".git" / "PR_BODY.md"
    git_internal.write_text("# stash residue\n", encoding="utf-8")

    files = {p.resolve() for p in _collect_md_files(tmp_path)}

    assert all(".git" not in f.parts for f in files), (
        f"unexpected .git/ entry in collection: "
        f"{[str(f) for f in files if '.git' in f.parts]}"
    )


def test_falls_back_outside_git_checkout(tmp_path: Path) -> None:
    """When ``repo_root`` is not a git checkout, fall back to glob.

    This preserves the script's defensive posture for downstream
    consumers running it from a tarball / extracted source dir without
    a ``.git`` directory.
    """
    (tmp_path / "README.md").write_text("# root\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")

    # No git init — must not raise; must still find the obvious files.
    files = {p.resolve() for p in _collect_md_files(tmp_path)}
    assert (tmp_path / "README.md").resolve() in files
    assert (tmp_path / "docs" / "guide.md").resolve() in files


def _init_git_repo(path: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=path, check=True, env=env,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path, check=True, env=env,
    )


def _git_add_commit(path: Path, target: str, message: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "add", target], cwd=path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=path, check=True, env=env,
    )


@pytest.fixture(autouse=True)
def _ensure_git_available() -> None:
    """Skip the suite cleanly if ``git`` is not on PATH."""
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):  # pragma: no cover
        pytest.skip("git not available on PATH")
