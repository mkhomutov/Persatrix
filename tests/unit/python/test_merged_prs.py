"""Pin ``scripts/merged_prs.py`` — the generated merged-PR history.

ROADMAP.md carried a hand-maintained "Merged PR History" table that stopped
at #708 in June while ~150 PRs merged after it: the table is derivable from
the squash-merge subjects on ``main``, and derivable data that is typed by
hand goes stale. The generator reads ``git log`` and writes
``docs/merged-prs.md`` between auto markers, the same shape as the RFC and
issue indexes, so a ``--check`` can fail CI when it is stale.

A shallow clone's log stops at its oldest commit (cloud sessions clone with
``--depth 50``), so a table generated from it alone loses every older row.
There the generator adds the committed rows older than the log; when the log
does not reach them, it and the pre-commit hook that runs it leave the file
alone and say why. The last tests run both in real clones, shallow and full.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from _git_fixture_helpers import git as _git
from _git_fixture_helpers import run as _run

from scripts._git import is_shallow
from scripts.checks import SKIPPED
from scripts.merged_prs import (
    REPO_ROOT,
    SHALLOW_NOTICE,
    MergedPR,
    behind_by,
    classify,
    compare,
    overlay,
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


def test_overlay_rebuilds_the_whole_table_from_a_shallow_log_and_the_committed_rows() -> None:
    """The log holds #5 to #3; the committed rows older than #3 supply the rest."""
    assert overlay(_table(3, 2, 1), _table(5, 4, 3)) == _table(5, 4, 3, 2, 1)
    assert overlay(_table(4, 3, 2, 1), _table(5, 4, 3)) == _table(5, 4, 3, 2, 1)


def test_overlay_takes_the_logs_rows_so_the_check_still_sees_a_hand_edit() -> None:
    edited = _table(4, 3, 2, 1).replace("feat: pr 4", "feat: pr four")
    whole = overlay(edited, _table(5, 4, 3))
    assert whole == _table(5, 4, 3, 2, 1)
    assert compare(edited, whole) is not None


def test_overlay_gives_up_when_the_log_stops_short_of_the_committed_rows() -> None:
    """Nothing says no row falls between #3, the log's oldest, and #2, the file's newest."""
    assert overlay(_table(2, 1), _table(5, 4, 3)) is None
    assert overlay("", _table(5, 4, 3)) is None
    assert overlay(_table(3, 2, 1), _table()) is None


def test_is_shallow_is_false_where_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a repository git fails; callers then keep their full-clone behaviour."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert is_shallow(tmp_path) is False


HISTORY = "docs/merged-prs.md"


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


def _full_history(origin: Path, tmp_path: Path) -> bytes:
    """The table a full clone writes: all four rows."""
    full = _clone(origin, tmp_path / "reference")
    assert _run(full, sys.executable, "scripts/merged_prs.py").returncode == 0
    return (full / HISTORY).read_bytes()


def test_a_shallow_clone_whose_log_stops_short_neither_rewrites_nor_passes_the_history(
    origin: Path, tmp_path: Path
) -> None:
    """The clone's log holds only #4, newer than every committed row: the rows between are unknown.

    Writing would lose every older row, and a check that passed would let the
    release sweep record a gate that never ran. Both leave the file alone,
    say why, and exit ``SKIPPED``.
    """
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1")
    assert is_shallow(shallow)
    committed = (shallow / HISTORY).read_bytes()

    for flags in ([], ["--check"]):
        proc = _run(shallow, sys.executable, "scripts/merged_prs.py", *flags)
        assert (proc.returncode, proc.stdout, proc.stderr) == (SKIPPED, "", f"{SHALLOW_NOTICE}\n")
    assert (shallow / HISTORY).read_bytes() == committed


def test_a_shallow_clone_whose_log_reaches_the_table_checks_and_rebuilds_all_of_it(
    origin: Path, tmp_path: Path
) -> None:
    """The log holds #4 and #3, the newest committed row: the older rows come from the file."""
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "2")

    assert _run(shallow, sys.executable, "scripts/merged_prs.py", "--check").returncode == 0
    proc = _run(shallow, sys.executable, "scripts/merged_prs.py")
    assert (proc.returncode, proc.stdout) == (0, f"wrote {HISTORY} (4 row(s))\n")
    assert (shallow / HISTORY).read_bytes() == _full_history(origin, tmp_path)


def test_a_shallow_clone_still_fails_a_hand_edit_its_log_can_see(
    origin: Path, tmp_path: Path
) -> None:
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "2")
    path = shallow / HISTORY
    path.write_text(path.read_text(encoding="utf-8").replace("change 3", "change three"),
                    encoding="utf-8")

    proc = _run(shallow, sys.executable, "scripts/merged_prs.py", "--check")
    assert proc.returncode == 1 and "hand edit" in proc.stderr


def test_a_full_clone_checks_and_regenerates_the_history_as_before(
    origin: Path, tmp_path: Path
) -> None:
    """The committed table lacks only #4, which landed it: the check passes and a write adds it."""
    full = _clone(origin, tmp_path / "full")
    assert not is_shallow(full)

    assert _run(full, sys.executable, "scripts/merged_prs.py", "--check").returncode == 0
    proc = _run(full, sys.executable, "scripts/merged_prs.py")
    assert (proc.returncode, proc.stdout) == (0, f"wrote {HISTORY} (4 row(s))\n")


def test_the_hook_leaves_a_skipped_history_unstaged_and_says_why_last(
    origin: Path, tmp_path: Path
) -> None:
    """A generator that exits ``SKIPPED`` left its file alone, so the hook must not stage it.

    The working copy carries an unstaged edit: a ``git add`` would sweep it
    into the commit. The reason is repeated after the results, where it is
    not scrolled away, and so is plan status's, which skips there too.
    """
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1")
    path = shallow / HISTORY
    path.write_text(path.read_text(encoding="utf-8") + "a local note\n", encoding="utf-8")

    proc = _run(shallow, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert f"wrote {HISTORY}" not in proc.stdout
    assert "✗ merged prs" not in proc.stdout
    assert HISTORY not in _git(shallow, "diff", "--cached", "--name-only").splitlines()
    assert path.read_text(encoding="utf-8").endswith("a local note\n")
    summary = proc.stdout.split("Results:", 1)[1]
    assert f"skipped merged prs: {SHALLOW_NOTICE}" in summary
    assert "skipped plan status: [WARN] shallow clone" in summary


def test_the_hook_catches_up_a_shallow_clones_history_when_its_log_reaches_it(
    origin: Path, tmp_path: Path
) -> None:
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "2")

    proc = _run(shallow, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert f"wrote {HISTORY} (4 row(s))" in proc.stdout
    assert HISTORY in _git(shallow, "diff", "--cached", "--name-only").splitlines()
    assert (shallow / HISTORY).read_bytes() == _full_history(origin, tmp_path)


def test_the_hook_regenerates_and_stages_a_full_clones_history(
    origin: Path, tmp_path: Path
) -> None:
    """As before: the commit after a merge carries its row."""
    full = _clone(origin, tmp_path / "full")

    proc = _run(full, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert f"wrote {HISTORY} (4 row(s))" in proc.stdout
    assert "skipped merged prs" not in proc.stdout
    assert HISTORY in _git(full, "diff", "--cached", "--name-only").splitlines()
