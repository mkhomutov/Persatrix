"""Pin ``scripts/merged_prs.py`` — the generated merged-PR history.

ROADMAP.md carried a hand-maintained "Merged PR History" table that stopped
at #708 in June while ~150 PRs merged after it: the table is derivable from
the squash-merge subjects on ``main``, and derivable data that is typed by
hand goes stale. The generator reads ``git log`` and writes
``docs/merged-prs.md`` between auto markers, the same shape as the RFC and
issue indexes, so a ``--check`` can fail CI when it is stale.

A shallow clone's log stops at its oldest commit (cloud sessions clone with
``--depth 50``), so the table generated there loses every older row. The
generator, and the pre-commit hook that runs it, leave the file alone there
and say why; the last tests run both in real clones, shallow and full.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.merged_prs import (
    REPO_ROOT,
    MergedPR,
    behind_by,
    classify,
    compare,
    parse_log,
    render,
)

LOG = "\n".join(
    [
        "7403cd28|2026-09-06|docs(branching,claude): rewrite BRANCHING.md (#860)",
        "9d3d2878|2026-09-06|ci(checks): promote every local-only check into CI "
        "(ISSUE-0133, ISSUE-0134) (#858)",
        "89367eb2|2026-09-06|test(manual-tests,docs): v0.3.15 release-prep PR 1 — "
        "the ten-leg live arc (#855)",
        "9003032e|2026-09-02|fix(channels,wallet): ISSUE-0123 + ISSUE-0131 — "
        "the close reserve (v0.3.15 PR A4b) (#852)",
        "a6aeab86|2026-08-03|feat(v0313): RFC 0049 Phase 0 — metadata → DispatchContext (#809)",
        "deadbeef|2026-08-01|chore(deps): bump google.golang.org/grpc from 1.82.1 to 1.83.1 (#853)",
        "1f522469|2026-04-08|Initial commit",
        "cafef00d|2026-05-01|feat: a direct commit with no PR number",
    ]
)


def test_parse_log_keeps_only_squash_merged_prs_newest_first() -> None:
    prs = parse_log(LOG)
    assert [p.number for p in prs] == [860, 858, 855, 852, 809, 853]
    assert prs[0] == MergedPR(
        number=860, date="2026-09-06", title="docs(branching,claude): rewrite BRANCHING.md",
    )


def test_parse_log_strips_the_pr_suffix_from_the_title() -> None:
    assert all("(#" not in p.title for p in parse_log(LOG))


@pytest.mark.parametrize(
    "title, area",
    [
        ("feat(v0313): RFC 0049 Phase 0 — metadata → DispatchContext", "RFC 0049 · v0.3.13"),
        (
            "fix(channels,wallet): ISSUE-0123 + ISSUE-0131 — the close reserve (v0.3.15 PR A4b)",
            "ISSUE-0123, ISSUE-0131 · v0.3.15",
        ),
        (
            "test(manual-tests,docs): v0.3.15 release-prep PR 1 — the ten-leg live arc",
            "v0.3.15 release prep",
        ),
        (
            "docs(release): v0.3.14 post-release follow-up — Phase-4 backfills",
            "v0.3.14 post-release",
        ),
        ("chore(deps): bump google.golang.org/grpc from 1.82.1 to 1.83.1", "deps"),
        ("docs(branching,claude): rewrite BRANCHING.md", "branching, claude"),
        ("feat: scaffold initial project structure", "—"),
    ],
)
def test_classify_derives_the_area_column_from_the_title(title: str, area: str) -> None:
    assert classify(title) == area


def test_render_writes_the_table_between_auto_markers() -> None:
    content = render(parse_log(LOG))
    assert "<!-- BEGIN merged-prs:auto -->" in content
    assert "<!-- END merged-prs:auto -->" in content
    assert "| [#860](https://github.com/mkhomutov/Persatrix/pull/860) |" in content
    assert "6 merged" in content
    # Newest first, so #860 appears before #809.
    assert content.index("[#860]") < content.index("[#809]")


def test_render_escapes_pipes_in_titles() -> None:
    prs = [MergedPR(number=1, date="2026-01-01", title="feat: a | b")]
    assert "a \\| b" in render(prs)


def test_parse_log_keeps_one_row_per_pr_number() -> None:
    """A branch commit quoting a PR number must not add a second row for it."""
    log = "\n".join(
        [
            "aaaa|2026-09-07|fix: follow-up (#844)",
            "bbbb|2026-09-01|feat(memory): the real squash (#844)",
        ]
    )
    prs = parse_log(log)
    assert [p.number for p in prs] == [844]
    assert prs[0].title == "fix: follow-up"  # first (newest) occurrence wins


def _table(*numbers: int) -> str:
    return render([MergedPR(n, "2026-01-01", f"feat: pr {n}") for n in numbers])


def test_check_passes_when_the_file_is_behind_by_the_newest_merges() -> None:
    """The file can never contain the merge that lands it; a suffix is fresh enough."""
    assert behind_by(_table(3, 2, 1), _table(4, 3, 2, 1)) == 1
    assert behind_by(_table(3, 2, 1), _table(3, 2, 1)) == 0
    assert compare(_table(3, 2, 1), _table(5, 4, 3, 2, 1)) is None


def test_check_fails_on_a_hand_edit_or_a_lost_row() -> None:
    assert behind_by(_table(4, 2, 1), _table(4, 3, 2, 1)) is None  # lost a middle row
    assert behind_by(_table(4, 3, 2, 1, 0), _table(4, 3, 2, 1)) is None  # extra row
    edited = _table(3, 2, 1).replace("feat: pr 2", "feat: pr two")
    assert compare(edited, _table(3, 2, 1)) is not None


#: Fixture repositories commit as a fixed identity and take nothing from the
#: caller's git setup — no hooks, signing or templates, no GIT_DIR pointing
#: elsewhere. The scripts run under it too, since they call git themselves.
_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

HISTORY = "docs/merged-prs.md"
NOTICE = "shallow clone: run `git fetch --unshallow` to regenerate docs/merged-prs.md"


def _run(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=repo, capture_output=True, encoding="utf-8", check=False, env=_GIT_ENV
    )


def _git(repo: Path, *args: str) -> str:
    proc = _run(repo, "git", *args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture(scope="module")
def origin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repository holding this checkout's ``scripts/`` and four squash merges.

    The fourth commits the history of the first three: a table can never list
    the merge that lands it.
    """
    repo = tmp_path_factory.mktemp("origin")
    shutil.copytree(
        REPO_ROOT / "scripts", repo / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    (repo / "docs").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "chore: tooling")
    for n in (1, 2, 3):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"feat: change {n} (#{n})")
    assert _run(repo, sys.executable, "scripts/merged_prs.py").returncode == 0
    _git(repo, "add", HISTORY)
    _git(repo, "commit", "-q", "-m", "docs: the history so far (#4)")
    return repo


def _clone(origin: Path, dest: Path, *flags: str) -> Path:
    # A file:// URL, not a path: a local clone ignores --depth.
    _git(dest.parent, "clone", "-q", *flags, origin.as_uri(), dest.name)
    return dest


def test_a_shallow_clone_neither_rewrites_nor_fails_the_history(
    origin: Path, tmp_path: Path
) -> None:
    """The clone's log holds only #4, so the table it would write keeps one row of four.

    Writing that table loses every older row, and ``--check`` would call the
    committed one a hand edit. Both leave the file alone and say why.
    """
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1")
    committed = (shallow / HISTORY).read_bytes()

    for flags in ([], ["--check"]):
        proc = _run(shallow, sys.executable, "scripts/merged_prs.py", *flags)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, f"{NOTICE}\n", "")
    assert (shallow / HISTORY).read_bytes() == committed


def test_a_full_clone_checks_and_regenerates_the_history_as_before(
    origin: Path, tmp_path: Path
) -> None:
    """The committed table lacks only #4, which landed it: the check passes and a write adds it."""
    full = _clone(origin, tmp_path / "full")

    assert _run(full, sys.executable, "scripts/merged_prs.py", "--check").returncode == 0
    proc = _run(full, sys.executable, "scripts/merged_prs.py")
    assert (proc.returncode, proc.stdout) == (0, f"wrote {HISTORY} (4 row(s))\n")


def test_the_hook_leaves_a_shallow_clones_history_unstaged_and_says_why(
    origin: Path, tmp_path: Path
) -> None:
    """The pre-commit run skips the regeneration and the ``git add``, printing the same notice."""
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1")

    proc = _run(shallow, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert NOTICE in proc.stdout
    assert f"wrote {HISTORY}" not in proc.stdout
    assert _git(shallow, "status", "--porcelain", "--", HISTORY) == ""


def test_the_hook_regenerates_and_stages_a_full_clones_history(
    origin: Path, tmp_path: Path
) -> None:
    """As before: the commit after a merge carries its row."""
    full = _clone(origin, tmp_path / "full")

    proc = _run(full, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert f"wrote {HISTORY} (4 row(s))" in proc.stdout
    assert NOTICE not in proc.stdout
    assert HISTORY in _git(full, "diff", "--cached", "--name-only").splitlines()
