"""Regression tests for the worktree-safety of ``scripts/install_hooks.py``.

Both defects pinned here were live: committing from a *linked worktree* made
the shared ``.git/hooks/pre-commit`` resolve its repo root from ``$0``, which
points at the main checkout's hooks directory no matter which worktree is
committing. The hook then regenerated ``FILEMAP.md`` against the main
checkout's path while ``git ls-files``/``git add`` operated on the committing
worktree's index (``GIT_DIR``/``GIT_INDEX_FILE`` are inherited) — leaving the
main checkout dirty and holding the *other* tree's file map.

Both halves are pinned **behaviourally**, against a real repository with a
real linked worktree, because every cheaper oracle was tried and leaks:

* Asserting the hook *string* contains ``git rev-parse --show-toplevel``
  passes against a hook that only mentions it in a comment (the reason
  :func:`_hook_code` strips comments), and even comment-stripped it passes
  against a hook whose ``$0`` branch *unconditionally overwrites* the
  git-derived root — the original bug, restored, with a green suite. Line
  order is not the invariant; the ``if [ -z "$REPO_ROOT" ]`` guard is.
* Asserting the installer's *source text* mentions ``--git-path`` passes
  against an installer reverted to the hard-coded ``REPO_ROOT / ".git" /
  "hooks"`` that keeps an explanatory comment naming the git command.

So the content assertions below are kept only as fast, precise failure
localisation; :class:`TestHookResolvesCommittingWorktree` and
:class:`TestInstallerTargetsSharedHooksDir` are what actually hold the line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.install_hooks as install_hooks

REPO_ROOT = install_hooks.REPO_ROOT

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
requires_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")

# A stub stand-in for scripts/pre_commit.py. It records the two facts the
# original bug got wrong: the directory the hook cd-ed into, and which tree's
# copy of the script actually executed.
_MARKER_STUB = """\
import os
from pathlib import Path

marker = os.environ["HOOK_MARKER"]
root = Path(__file__).resolve().parent.parent
Path(marker).write_text(
    "cwd=%s\\nscript_root=%s\\n" % (os.getcwd(), root), encoding="utf-8"
)
"""


def _git(*args: str, cwd: Path) -> str:
    """Run git in ``cwd`` and return stripped stdout, raising on failure."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a repo plus a linked worktree; return ``(main, worktree)``.

    The repo carries a real copy of ``scripts/`` so the installer under test
    runs with its genuine imports rather than a stub package.
    """
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "test@example.invalid", cwd=main)
    _git("config", "user.name", "Test", cwd=main)
    shutil.copytree(REPO_ROOT / "scripts", main / "scripts")
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", "--no-verify", cwd=main)

    worktree = tmp_path / "wt"
    _git("worktree", "add", "-q", str(worktree), "-b", "wt-branch", cwd=main)
    # The precondition that broke the original code: a linked worktree's
    # `.git` is a gitdir *file*, so `REPO_ROOT / ".git" / "hooks"` is both
    # unwritable and a path git never reads.
    assert (worktree / ".git").is_file()
    return main, worktree


def _hook_code() -> str:
    """The hook's executable lines, comments stripped.

    Every content assertion must read the *code*: the hook carries a comment
    explaining why ``$0`` is wrong, and that comment names both ``$0`` and
    ``git rev-parse --show-toplevel`` verbatim. Asserting against the raw
    string lets a hook that only *documents* the fix pass.
    """
    return "\n".join(
        line
        for line in install_hooks.HOOK_CONTENT.splitlines()
        if not line.lstrip().startswith("#")
    )


class TestHookRootResolution:
    """Fast content checks on the generated hook (see module docstring)."""

    def test_prefers_git_show_toplevel(self) -> None:
        """``git rev-parse --show-toplevel`` is the primary resolution.

        It reads the inherited ``GIT_DIR``, so it names the committing
        worktree in both the main checkout and a linked one.
        """
        assert 'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"' in _hook_code()

    def test_dollar_zero_is_guarded_not_merely_later(self) -> None:
        """The ``$0`` form must sit *inside* an emptiness guard.

        Asserting only that the git-based line appears first is too weak: a
        hook that runs `git rev-parse` and then unconditionally reassigns
        ``REPO_ROOT`` from ``$0`` satisfies that ordering while restoring the
        original bug in full.
        """
        code = _hook_code()
        guard = 'if [ -z "$REPO_ROOT" ]; then'
        assert guard in code, "the $0 fallback must be guarded on an empty REPO_ROOT"

        lines = [line.strip() for line in code.splitlines()]
        guard_at = lines.index(guard)
        fi_at = lines.index("fi", guard_at)
        dollar_zero = [i for i, line in enumerate(lines) if 'dirname "$0"' in line]
        assert dollar_zero, 'expected a $0-derived fallback assignment'
        assert all(guard_at < i < fi_at for i in dollar_zero), (
            "$0-based root resolution must live inside the `if [ -z \"$REPO_ROOT\" ]` "
            "branch; outside it, it overwrites the git-derived root and reinstates "
            "the main-checkout bug"
        )

    @requires_bash
    def test_hook_script_is_valid_bash(self) -> None:
        proc = subprocess.run(
            ["bash", "-n"],
            input=install_hooks.HOOK_CONTENT,
            text=True,
            capture_output=True,
        )
        assert proc.returncode == 0, proc.stderr


@requires_git
@requires_bash
class TestHookResolvesCommittingWorktree:
    """End-to-end: the installed hook must act on the *committing* tree."""

    def test_commit_from_worktree_runs_in_that_worktree(self, tmp_path: Path) -> None:
        """Commit from a linked worktree; the hook must land there, not in main.

        This is the actual regression. With the ``$0`` form the hook cd-ed to
        the main checkout and ran *its* copy of the script, so the generated
        file map described the wrong tree.
        """
        main, worktree = _init_repo_with_worktree(tmp_path)

        # Both trees carry the stub, so the marker proves which one ran.
        for tree in (main, worktree):
            (tree / "scripts" / "pre_commit.py").write_text(_MARKER_STUB, encoding="utf-8")

        hook = main / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        with hook.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(install_hooks.HOOK_CONTENT)
        hook.chmod(0o755)

        marker = tmp_path / "marker.txt"
        env = {**os.environ, "HOOK_MARKER": str(marker), "PYTHON": sys.executable}
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        env.pop("GIT_INDEX_FILE", None)

        (worktree / "change.txt").write_text("hi\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, env=env)
        proc = subprocess.run(
            ["git", "commit", "-m", "from the worktree"],
            cwd=worktree,
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, f"commit failed:\n{proc.stdout}\n{proc.stderr}"

        assert marker.exists(), "the pre-commit hook did not run"
        recorded = dict(
            line.split("=", 1) for line in marker.read_text(encoding="utf-8").splitlines()
        )
        expected = str(worktree.resolve())
        assert recorded["cwd"] == expected, (
            f"hook ran in {recorded['cwd']!r}; expected the committing worktree {expected!r}"
        )
        assert recorded["script_root"] == expected, (
            f"hook executed {recorded['script_root']!r}'s copy of the script; "
            f"expected the committing worktree {expected!r}"
        )


@requires_git
class TestInstallerTargetsSharedHooksDir:
    """The installer must write where git actually looks for hooks."""

    def test_install_from_worktree_writes_to_the_common_dir(self, tmp_path: Path) -> None:
        """Running the installer *from a linked worktree* must succeed.

        Pinned behaviourally rather than by grepping the installer's source:
        a revert to the hard-coded ``REPO_ROOT / ".git" / "hooks"`` that keeps
        a comment naming the git command passes a source-text assertion, but
        fails here — in a worktree that path is a file, so the install raises
        ``NotADirectoryError`` instead of writing anything.
        """
        main, worktree = _init_repo_with_worktree(tmp_path)

        proc = subprocess.run(
            [sys.executable, str(worktree / "scripts" / "install_hooks.py"), "--force"],
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"installer failed:\n{proc.stdout}\n{proc.stderr}"

        shared = main / ".git" / "hooks" / "pre-commit"
        assert shared.is_file(), "hook did not land in the shared common dir"
        assert install_hooks.HOOK_CONTENT in shared.read_text(encoding="utf-8")
        assert not (worktree / ".git" / "hooks").exists(), (
            "installer wrote beside the worktree's .git file instead of the common dir"
        )

    def test_honours_core_hooks_path(self, tmp_path: Path) -> None:
        """``core.hooksPath`` redirects hook lookup, so the installer must follow.

        ``_hooks_dir``'s docstring promises this. Asserting the resolved
        directory is literally named ``hooks`` would contradict it and fail
        for any contributor whose config sets the override.
        """
        main, _ = _init_repo_with_worktree(tmp_path)
        custom = main / "custom-githooks"
        _git("config", "core.hooksPath", str(custom), cwd=main)

        proc = subprocess.run(
            [sys.executable, str(main / "scripts" / "install_hooks.py"), "--force"],
            cwd=main,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"installer failed:\n{proc.stdout}\n{proc.stderr}"
        assert (custom / "pre-commit").is_file(), "installer ignored core.hooksPath"
        assert not (main / ".git" / "hooks" / "pre-commit").exists()
