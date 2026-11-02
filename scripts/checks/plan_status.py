#!/usr/bin/env python3
"""Flag progress-table rows that say "PR open" for a PR that has merged.

The most common status-hygiene defect in this repository's history: a PR
merges and the plan row that announced it — ``🔀 PR open ([#855](…))`` — is
never flipped to ``✅ Merged``. Every open cycle's plan, plus every RFC and
issue-owned PR plan, carries such a table. This check reads those tables and
reports a row whose status cell
starts with 🔀 (PR open) or ⬜ (not started) while **every** PR it links to
is already a squash-merge subject on ``main``.

A PR usually writes its own row before its number exists, so that row links
nothing — ``🔀 PR open | — (this PR) |``. Such a row is judged by the commit
that wrote it instead: once that commit is a squash-merge subject, the PR
has merged and the row is stale. While the PR is open the line comes from a
branch commit or the working tree, so the row passes.

🔄 (in progress) cells are left alone: they legitimately mix merged and open
PRs ("PR 0 merged (#854), PR 1 open (#855)"). Released versions' plans are
frozen evidence and are skipped; which versions are released is read from
``CHANGELOG.md`` the same way the size checker does.

A PR cannot write ✅ and a merge date before it has merged, so its own row
still says 🔀 when it lands (ISSUE-0190). ``docs/merged-prs.md`` can never hold
the merge that lands it either, so the row follows that file: a 🔀 row passes
while the history has no row for its PR, which is exactly the lag
``merged_prs.py --check`` allows. Nothing narrower will do. About one merge in
four lands with no row for the merge before it (Dependabot, or a branch with
no commit the hook ran on since), so a rule that spared the newest merge alone
would fail the row one merge later.

``--fix`` is the other half: it flips what the check would fail, with the PR's
link and the date the history gives, which is the squash commit's local date
and not ``mergedAt`` in UTC. It writes the staged copy as well as the working
copy, and the hook runs ``merged_prs.py`` and then this, so the commit that
adds a merge's history row always carries its flip. A row the writer cannot
flip without losing something is left for a person, and the check still fails it.

A shallow clone's log stops at its oldest commit (cloud sessions clone with
``--depth 50``), so a PR merged before it looks open and its stale row would
pass. There no row is judged: a notice names ``git fetch --unshallow`` and the
check exits 3 (``SKIPPED``), which the hook reports as a skip and CI and the
release sweep as a failure. The guard against a plan that hides its rows and
the one-document rule below read the tree, so they still run and still fail.

The check also holds a patch release to one document. Since ruling (e) of the
sequencing Amendment 2026-09-12 a patch release keeps its plan, scope locks
and release checklist in ``docs/vX.Y.Z-plan.md``, so any other
``docs/vX.Y.Z-*.md`` file for a patch version after v0.3.16 fails, except a
test-findings PR plan. v0.3.16 and earlier keep the files they shipped with,
and a minor release (``X.Y.0``) is not judged.

Usage::

    python scripts/checks/plan_status.py [--verbose]
    python scripts/checks/plan_status.py --fix    # flip stale rows in place
    python scripts/checks/plan_status.py --staged # judge what the commit holds (the hook)

Exit code: 0 clean, 1 if any stale row, if a plan opens a code fence or HTML
comment that never closes — every row after it is off the page, so none of
them would be judged — or if a patch release has a second document; 3 in a
shallow clone that has none of those failures. ``--fix`` exits 0; 3 without
writing while a merge conflict is unresolved or ``git diff`` fails; 1 when git
would not stage a flip.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import NamedTuple, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._git import git_output, is_shallow  # noqa: E402
from scripts.checks import SKIPPED, ensure_utf8_stdout, markdown_page  # noqa: E402
from scripts.checks.plan_row_flip import (  # noqa: E402
    PR_LINK_RE,
    STALE_LEADERS,
    StaleRow,
    can_flip,
    flip_rows,
    stage,
    staged_bytes,
)
from scripts.checks.released import is_released_version_doc, released_versions  # noqa: E402
from scripts.merged_prs import merge_dates  # noqa: E402

#: A version-cycle file: groups 1-3 are the version's numbers, group 4 its kind.
_VERSION_DOC_NAME_RE = re.compile(r"^docs/v(\d+)\.(\d+)\.(\d+)-(.+)\.md$")
#: The only kinds a patch release may keep (sequencing Amendment 2026-09-12,
#: ruling (e)): the plan, which holds the scope locks and the release
#: checklist, and a test-findings PR plan, which is a PR plan. The execution
#: report lives under ``docs/manual-tests/``, so it is never read here. Any other
#: kind (scope locks, a checklist, a split-out PR plan or risks table) is a
#: second document — an allowlist, so a new name cannot slip past.
_ONE_DOCUMENT_KINDS = frozenset({"plan", "test-findings-pr-plan"})
#: The last release that wrote several documents. A fixed version, not a dated
#: CHANGELOG heading, so the tag PR that dates a release is still judged.
_LAST_RELEASE_BEFORE_THE_RULING = (0, 3, 16)
_LINK_ONLY_CELL_RE = re.compile(r"^\[#(\d+)\]\([^)]+\)$")
_SUBJECT_RE = re.compile(r"\(#(\d+)\)$")
#: The committer of every squash merge made on GitHub, the web page and the API alike.
_GITHUB_COMMITTER = "noreply@github.com"
_HISTORY = "docs/merged-prs.md"
_T = TypeVar("_T")


def _subject_numbers(log: str | None) -> frozenset[int]:
    """The PR numbers that end the subjects in *log*."""
    numbers = set()
    for subject in (log or "").splitlines():
        m = _SUBJECT_RE.search(subject.strip())
        if m:
            numbers.add(int(m.group(1)))
    return frozenset(numbers)


def merged_pr_numbers(repo_root: Path) -> frozenset[int]:
    """PR numbers squash-merged on the history reachable from HEAD."""
    return _subject_numbers(git_output(repo_root, "log", "--first-parent", "--format=%s"))


def prs_named_by_this_branch(repo_root: Path) -> frozenset[int]:
    """PR numbers that end the subjects of this branch's own commits, which prove no merge.

    ``fix: review follow-up (#3)`` reads like a squash merge to the log walks
    here and in ``merged_prs.py``. The check's mistake is loud, since it fails
    the row. A flip written on that word would be quiet: nothing checks a ✅.
    A commit GitHub made is a squash merge wherever it sits: ``origin/main`` can
    lag the ``main`` a branch was rebased on (a fork pulls from ``upstream``),
    and the merges in between are not this branch's. Empty without
    ``origin/main``, where the two cannot be told apart.
    """
    log = git_output(
        repo_root, "log", "--first-parent", "--format=%ce%x00%s", "origin/main..HEAD"
    )
    commits = (line.partition("\0") for line in (log or "").splitlines())
    return _subject_numbers(
        "\n".join(subject for email, _, subject in commits if email != _GITHUB_COMMITTER)
    )


def pr_that_wrote(
    repo_root: Path, rel: str, lineno: int, contents: Path | None = None,
) -> int | None:
    """The PR whose squash-merge last wrote line *lineno* of *rel*, or ``None``.

    ``git blame --first-parent`` walks the same history as
    ``merged_pr_numbers``. A line from the working tree or a branch commit
    has no PR: that is a PR's own row while the PR is still open. Nor does a
    boundary commit — a shallow clone's oldest commit, or the root — which
    blame credits with every older line, whoever wrote it. *contents* blames
    that file's copy of *rel*, not the working copy.
    """
    copy = ("--contents", str(contents)) if contents else ()
    out = git_output(
        repo_root, "blame", "--first-parent", "--line-porcelain", *copy,
        "-L", f"{lineno},{lineno}", "--", rel,
    )
    if out is None:
        return None
    fields = out.split("\n")
    if "boundary" in fields:
        return None
    summary = next((f.removeprefix("summary ") for f in fields if f.startswith("summary ")), "")
    m = _SUBJECT_RE.search(summary.strip())
    return int(m.group(1)) if m else None


def find_stale_rows(
    text: str,
    merged: frozenset[int],
    written_by: Callable[[int], int | None] | None = None,
) -> list[StaleRow]:
    """Rows whose 🔀 / ⬜ status cell announces PRs that have all merged.

    For a 🔀 row every PR link on the row counts (the status cell names the
    open PR, or a dedicated "GitHub PR" column does). A 🔀 row that links
    nothing is a PR's own row: *written_by* maps its line number to the PR
    whose squash-merge wrote it, and that PR counts. For a ⬜ row only a cell
    that is *nothing but* a PR link counts — a not-started row that merely
    cites a merged PR in prose ("after #844 lands") is not stale. A row inside
    a code fence or an HTML comment is not on the page, so it is not judged.
    """
    stale: list[StaleRow] = []
    # Split on "\n" alone, as git does, so line n here is line n to git blame.
    for lineno, line in enumerate(markdown_page.rendered(text.split("\n")), start=1):
        cells = markdown_page.cells(line)
        if not cells:
            continue
        status = next((c for c in cells if c.startswith(STALE_LEADERS)), None)
        if status is None:
            continue
        writer: int | None = None
        if status.startswith("🔀"):
            linked = {int(n) for n in PR_LINK_RE.findall(line)}
            if not linked and written_by is not None:
                writer = written_by(lineno)
                linked = set() if writer is None else {writer}
        else:
            linked = {
                int(m.group(1)) for c in cells if (m := _LINK_ONLY_CELL_RE.match(c))
            }
        if not linked or not linked <= merged:
            continue
        stale.append(StaleRow("", lineno, tuple(sorted(linked)), status[:60], writer is not None))
    return stale


def recorded_merges(repo_root: Path, staged: bool = False) -> dict[int, str]:
    """PR number → date, for each PR with a row in ``docs/merged-prs.md``, or its staged copy."""
    if staged:
        return merge_dates(staged_bytes(repo_root, _HISTORY).decode("utf-8", "replace"))
    path = repo_root / _HISTORY
    return merge_dates(path.read_text(encoding="utf-8")) if path.exists() else {}


def trails_the_history(row: StaleRow, recorded: Mapping[int, str]) -> bool:
    """Whether *row* is a 🔀 row whose PR merged after ``docs/merged-prs.md`` was written.

    A ⬜ row gets no lag: a merge leaves its row at 🔀. Nor does anything when
    the history has no rows: every merge would look newer than it, so a lost
    or reformatted file would pass every row.
    """
    return bool(recorded) and row.cell.startswith("🔀") and not recorded.keys() >= set(row.merged)


def _stale_rows_in(
    repo_root: Path, rel: str, text: str, merged: frozenset[int], contents: Path | None = None,
) -> list[StaleRow]:
    written_by = partial(pr_that_wrote, repo_root, rel, contents=contents)
    return [s._replace(file=rel) for s in find_stale_rows(text, merged, written_by)]


def _judged(
    repo_root: Path, rel: str, merged: frozenset[int] | None, text: str,
    contents: Path | None = None,
) -> tuple[str, list[StaleRow]]:
    """*text*, and its stale rows; none when *merged* is ``None`` (a shallow clone)."""
    return text, [] if merged is None else _stale_rows_in(repo_root, rel, text, merged, contents)


class _Facts(NamedTuple):
    """What the writer goes by, the same for every copy of every plan."""

    recorded: Mapping[int, str]
    merged: frozenset[int]
    named_by_branch: frozenset[int]


class _Flip(NamedTuple):
    """One copy of a plan, before and after its rows were flipped."""

    before: str
    after: str
    flipped: list[StaleRow]
    left: list[StaleRow]


def _flip_copy(
    repo_root: Path, rel: str, text: str, facts: _Facts, contents: Path | None = None,
) -> _Flip:
    stale = [
        s for s in _stale_rows_in(repo_root, rel, text, facts.merged, contents)
        if not trails_the_history(s, facts.recorded)
    ]
    flippable = [s for s in stale if not facts.named_by_branch & set(s.merged)]
    after, left = flip_rows(text, flippable, facts.recorded)
    flipped = [s for s in flippable if s not in left]
    return _Flip(text, after, flipped, left + [s for s in stale if s not in flippable])


def _with_staged_copy(repo_root: Path, rel: str, use: Callable[[str, Path], _T]) -> _T:
    """*use* called with the staged copy of *rel*: its text, and a file git blame can read."""
    blob = staged_bytes(repo_root, rel)
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "staged"
        copy.write_bytes(blob)
        return use(blob.decode("utf-8", "surrogateescape"), copy)


def _flip_staged_copy(repo_root: Path, rel: str, facts: _Facts) -> _Flip:
    """The staged copy of a plan whose working copy differs, flipped on its own terms.

    Its rows sit on other lines, and it may hold a row the working copy has
    already flipped: ``git commit <path>`` leaves the old row in the real index,
    where the next commit would undo the flip.
    """
    return _with_staged_copy(
        repo_root, rel, lambda text, copy: _flip_copy(repo_root, rel, text, facts, copy)
    )


def _report(copy: _Flip, named_by_branch: frozenset[int], where: str = "") -> None:
    for s in copy.flipped:
        print(f"flipped {s.file}:{s.line}{where} to ✅ ({s.prs})")
    for s in copy.left:
        why = "flip it by hand"
        if named_by_branch & set(s.merged):
            why = "a commit on this branch ends in its number, which proves no merge"
        print(f"[WARN] {s.file}:{s.line}{where}: {s.cell!r} ({s.prs}) left as it is — {why}")


def fix_stale_rows(repo_root: Path) -> int:
    """Flip every row the check would fail, working copy and staged copy, and report the rest.

    Both copies, because the commit takes the staged one: it must carry the
    flip whatever else is unstaged, or it adds the history row with the row
    still open, and CI fails it. A row the history has no date for yet is one
    the check passes, so run ``merged_prs.py`` first, as the hook does.

    A shallow clone is no bar: a row is flipped only for a PR the history
    records, and those have merged however short the log is. Nothing is written
    while a conflict is unresolved: a conflicted plan holds both sides. ``git
    commit`` refuses before the hook in that state, so this guards a hand run.
    Nor when ``git diff`` fails: which plans hold unstaged edits is unknown, and
    staging the working copy would sweep them into the commit. 1 when git would
    not stage a flip.
    """
    unmerged = git_output(repo_root, "ls-files", "--unmerged")
    if unmerged is None or unmerged:
        print("[WARN] a merge conflict is unresolved — no plan row flipped;"
              " resolve it, and the next commit carries the flip")
        return SKIPPED
    diff = git_output(repo_root, "diff", "--name-only")
    if diff is None:
        print("[WARN] git cannot say which plans have unstaged edits — no plan row flipped;"
              " the next commit carries the flip")
        return SKIPPED
    recorded = recorded_merges(repo_root)
    facts = _Facts(
        recorded, merged_pr_numbers(repo_root) | recorded.keys(),
        prs_named_by_this_branch(repo_root),
    )
    differ = set(diff.splitlines())
    result = 0
    for p in target_docs(repo_root):
        rel = p.relative_to(repo_root).as_posix()
        # surrogateescape, and bytes both ways: every byte but the row's stays as it was.
        text = p.read_bytes().decode("utf-8", "surrogateescape")
        working = _flip_copy(repo_root, rel, text, facts)
        if working.after != working.before:
            p.write_bytes(working.after.encode("utf-8", "surrogateescape"))
        _report(working, facts.named_by_branch)
        staged = working
        if rel in differ:
            staged = _flip_staged_copy(repo_root, rel, facts)
            _report(staged, facts.named_by_branch, " (staged copy)")
        if staged.after != staged.before and not stage(repo_root, rel, staged.after):
            print(f"[FAIL] {rel}: git would not stage the flip, so the commit would carry"
                  " the history row with the row still open — run `make plan-status-fix`")
            result = 1
    return result


def target_docs(repo_root: Path) -> list[Path]:
    """Open-cycle plans + every RFC / issue PR plan."""
    released = released_versions(repo_root)
    docs: list[Path] = []
    # ``v*-plan.md`` also matches ``v*-release-prep-plan.md`` and the two
    # ``v*-test-findings-pr-plan.md`` files; one glob, deduped, frozen ones out.
    for p in sorted(set((repo_root / "docs").glob("v*-plan.md"))):
        rel = p.relative_to(repo_root).as_posix()
        if not is_released_version_doc(rel, released):
            docs.append(p)
    docs += sorted((repo_root / "docs" / "rfcs").glob("*pr-plan*.md"))
    docs += sorted((repo_root / "docs" / "issues").glob("*pr-plan*.md"))
    return docs


def second_release_documents(repo_root: Path) -> list[str]:
    """Release documents beside a patch release's plan, for a version after v0.3.16.

    Only a patch release (``X.Y.Z`` with ``Z`` above 0) is held to one document:
    the ruling names patch releases, and the amendment that opens a minor
    release decides its shape. v0.3.16 and every release before it wrote
    several and keep them.
    """
    found = []
    for path in (repo_root / "docs").glob("v*.md"):
        rel = path.relative_to(repo_root).as_posix()
        match = _VERSION_DOC_NAME_RE.match(rel)
        if not match or match.group(4) in _ONE_DOCUMENT_KINDS:
            continue
        version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if version[2] > 0 and version > _LAST_RELEASE_BEFORE_THE_RULING:
            found.append(rel)
    return sorted(found)


def check_plan_status(repo_root: Path, verbose: bool = False, staged: bool = False) -> int:
    """*staged* judges each plan's staged copy, which is what a commit holds (the hook)."""
    extra = second_release_documents(repo_root)
    if extra:
        print(f"[FAIL] {len(extra)} release document(s) beside a patch release's plan"
              " — a patch release keeps one document (sequencing Amendment 2026-09-12,"
              " ruling (e)):")
        for rel in extra:
            # The version holds no hyphen, so the first one ends it: docs/vX.Y.Z-plan.md.
            print(f"  {rel}: fold it into {rel.split('-', 1)[0]}-plan.md")
    shallow = is_shallow(repo_root)
    if shallow:
        print("[WARN] shallow clone — skipping the stale-row check; run `git fetch --unshallow`"
              " to check plan rows against every merged PR")
        merged: frozenset[int] = frozenset()
    else:
        merged = merged_pr_numbers(repo_root)
        if not merged:
            print("[WARN] no squash-merge subjects found in git log — skipping the stale-row check")
            return 1 if extra else 0
    stale: list[StaleRow] = []
    docs = target_docs(repo_root)
    hidden: list[str] = []
    texts: dict[str, str] = {}
    for p in docs:
        rel = p.relative_to(repo_root).as_posix()
        judge = partial(_judged, repo_root, rel, None if shallow else merged)
        if staged:
            text, rows = _with_staged_copy(repo_root, rel, judge)
        else:
            # Not read_text(): it turns a lone "\r" into a line end git blame does not count.
            text, rows = judge(p.read_bytes().decode("utf-8", errors="replace"))
        texts[rel] = text
        stale += rows
        opened = markdown_page.unclosed(text.split("\n"))
        if opened is not None:
            hidden.append(f"{rel}:{opened + 1}")
        if verbose:
            print(f"  scanned {rel}")
    if not shallow:
        print(f"[SCAN] Checked progress tables in {len(docs)} plan(s) against {len(merged)}"
              " merged PRs")
    recorded = recorded_merges(repo_root, staged)
    trailing = [s for s in stale if trails_the_history(s, recorded)]
    stale = [s for s in stale if s not in trailing]
    for s in trailing:
        then = (
            "the next commit flips the row" if can_flip(texts[s.file], s)
            else "the writer cannot flip this row, so flip it by hand"
        )
        print(f"[NOTE] {s.file}:{s.line}: {s.prs} merged after docs/merged-prs.md was written"
              f" — {then}")
    if hidden:
        print(f"\n[FAIL] {len(hidden)} plan(s) hide every row after a code fence or HTML comment"
              " that never closes:")
        for where in hidden:
            print(f"  {where}: opened here — close it")
    if stale:
        print(f"\n[FAIL] {len(stale)} stale row(s) — the PR merged but the row still says open:")
        for s in stale:
            if s.unlinked:
                print(f"  {s.file}:{s.line}: {s.cell!r} links no PR; {s.prs} wrote it and has"
                      " merged — flip the row and link its PR")
            else:
                print(f"  {s.file}:{s.line}: {s.cell!r} links {s.prs}, all merged — flip the row")
    if stale or hidden or extra:
        return 1
    if shallow:
        return SKIPPED
    print("[OK] No plan row announces an open PR that has already merged, and no patch"
          " release has a second release document.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Flag stale 'PR open' rows in plan tables and a second release document.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--fix", action="store_true",
        help="Flip stale rows to ✅ with the PR's link and its docs/merged-prs.md date,"
             " in the working copy and the staged copy.",
    )
    parser.add_argument(
        "--staged", action="store_true",
        help="Judge each plan's staged copy, as the commit will hold it (the hook does).",
    )
    args = parser.parse_args(argv)
    if args.fix:
        return fix_stale_rows(REPO_ROOT)
    return check_plan_status(REPO_ROOT, verbose=args.verbose, staged=args.staged)


if __name__ == "__main__":
    raise SystemExit(main())
