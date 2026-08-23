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
import time
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


def _stale_hook_warning_in(repo: Path) -> str | None:
    """Evaluate ``pre_commit._stale_hook_warning()`` inside ``repo``.

    Out of process on purpose: both the warning and the hook path it compares
    against are anchored to the *importing* tree's ``REPO_ROOT``, so asking
    the question about a temp repo means asking from inside it.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, '.'); "
            "from scripts.pre_commit import _stale_hook_warning; "
            "w = _stale_hook_warning(); "
            "print(w if w is not None else '', end='')",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout or None


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

    def test_unresolvable_hooks_dir_fails_cleanly(self, tmp_path: Path) -> None:
        """No git means a clean exit 1, not a traceback.

        The tempting fallback — ``REPO_ROOT / ".git" / "hooks"`` — is wrong in
        precisely this situation: from a linked worktree ``.git`` is a file, so
        creating ``hooks`` beneath it raises ``NotADirectoryError``. Resolving
        to ``None`` and reporting it keeps the failure legible.
        """
        _, worktree = _init_repo_with_worktree(tmp_path)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()

        proc = subprocess.run(
            [sys.executable, str(worktree / "scripts" / "install_hooks.py"), "--force"],
            cwd=worktree,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(empty_bin)},
        )
        assert proc.returncode == 1, f"expected a clean failure, got:\n{proc.stdout}"
        assert "Traceback" not in proc.stderr, f"crashed instead of reporting:\n{proc.stderr}"
        assert "NotADirectoryError" not in proc.stderr
        assert "could not resolve" in proc.stderr.lower()

    def test_ignores_an_inherited_git_dir(self, tmp_path: Path) -> None:
        """``GIT_DIR`` in the environment must not redirect the install.

        git honours the variable over ``cwd``, and every process spawned from
        inside a hook, ``git rebase -x`` or ``git bisect run`` inherits it — so
        without sanitising, the installer writes into whichever repository git
        was already operating on.
        """
        main, _ = _init_repo_with_worktree(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        _git("init", "-q", cwd=other)

        proc = subprocess.run(
            [sys.executable, str(main / "scripts" / "install_hooks.py"), "--force"],
            cwd=main,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_DIR": str(other / ".git")},
        )
        assert proc.returncode == 0, f"installer failed:\n{proc.stdout}\n{proc.stderr}"
        assert (main / ".git" / "hooks" / "pre-commit").is_file()
        assert not (other / ".git" / "hooks" / "pre-commit").exists(), (
            "inherited GIT_DIR redirected the install into an unrelated repository"
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


@requires_git
@requires_bash
class TestHookToleratesBranchesWithoutTheCheckScript:
    """The hook is shared across worktrees; the script it runs is not."""

    def test_commit_succeeds_on_a_branch_without_pre_commit_py(self, tmp_path: Path) -> None:
        """A branch with no ``scripts/pre_commit.py`` must still be committable.

        The hook lives in the shared common dir and therefore runs for every
        worktree, but it delegates to tracked content that varies per branch.
        Without the guard the commit is rejected by a bare interpreter error
        naming a file the author never expected to need.
        """
        main, _ = _init_repo_with_worktree(tmp_path)
        default_branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=main)

        _git("checkout", "-q", "--orphan", "docs-only", cwd=main)
        _git("rm", "-r", "-f", "-q", ".", cwd=main)
        (main / "README.md").write_text("docs\n", encoding="utf-8")
        _git("add", "-A", cwd=main)
        _git("commit", "-qm", "docs-only branch", "--no-verify", cwd=main)
        _git("checkout", "-q", default_branch, cwd=main)

        docs_tree = tmp_path / "docs-wt"
        _git("worktree", "add", "-q", str(docs_tree), "docs-only", cwd=main)
        assert not (docs_tree / "scripts").exists()

        hook = main / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        with hook.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(install_hooks.HOOK_CONTENT)
        hook.chmod(0o755)

        env = {**os.environ, "PYTHON": sys.executable}
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(var, None)

        (docs_tree / "README.md").write_text("docs\nmore\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=docs_tree, check=True, env=env)
        proc = subprocess.run(
            ["git", "commit", "-m", "edit from the docs-only worktree"],
            cwd=docs_tree,
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, f"commit was rejected:\n{proc.stdout}\n{proc.stderr}"
        assert "skipping checks" in proc.stderr
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=docs_tree) == "docs-only"


@requires_git
class TestStaleHookDetection:
    """A hook that drifts from the installer must be reported.

    The hook is generated into ``.git/`` and is not version-controlled, so
    pulling a fix to ``scripts/install_hooks.py`` leaves the old one running.
    ``scripts/pre_commit.py`` is the only version-controlled thing that runs
    on every commit, so it is where the drift can be noticed.
    """

    def test_reports_a_hook_that_differs_from_the_installer(self, tmp_path: Path) -> None:
        main, _ = _init_repo_with_worktree(tmp_path)
        hook = main / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/usr/bin/env bash\n# an older generation\n", encoding="utf-8")

        warning = _stale_hook_warning_in(main)
        assert warning is not None, "drifted hook was not reported"
        assert "install_hooks.py --force" in warning

    def test_silent_when_the_hook_is_current(self, tmp_path: Path) -> None:
        main, _ = _init_repo_with_worktree(tmp_path)
        proc = subprocess.run(
            [sys.executable, str(main / "scripts" / "install_hooks.py"), "--force"],
            cwd=main,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert _stale_hook_warning_in(main) is None

    def test_silent_when_no_hook_is_installed(self, tmp_path: Path) -> None:
        """Running the checks by hand in a checkout without the hook is normal."""
        main, _ = _init_repo_with_worktree(tmp_path)
        assert not (main / ".git" / "hooks" / "pre-commit").exists()
        assert _stale_hook_warning_in(main) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim")
class TestGitCallIsBounded:
    """A wedged git must not hang the caller indefinitely."""

    def test_hanging_git_times_out_and_reports_no_hooks_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``subprocess.TimeoutExpired`` is a ``SubprocessError``, so a wedged
        git lands in the same "could not resolve" path as a missing one.
        """
        shim = tmp_path / "bin"
        shim.mkdir()
        fake_git = shim / "git"
        # Python shebang, not `#!/bin/sh` + `sleep`: PATH is replaced below, so
        # a shell shim cannot resolve its own commands and would exit 127
        # instantly — passing this test for entirely the wrong reason.
        fake_git.write_text(
            f"#!{sys.executable}\nimport time\n\ntime.sleep(30)\n", encoding="utf-8"
        )
        fake_git.chmod(0o755)

        monkeypatch.setenv("PATH", str(shim))
        monkeypatch.setattr(install_hooks, "_GIT_TIMEOUT_S", 0.5)

        started = time.monotonic()
        assert install_hooks._hooks_dir() is None
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"did not honour the timeout (took {elapsed:.1f}s)"
        # Guard against the shim failing fast and passing this vacuously.
        assert elapsed >= 0.5, f"git shim exited in {elapsed:.2f}s — it never blocked"

    def test_undecodable_git_output_does_not_escape_the_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bytes that are not valid UTF-8 must not crash the caller.

        ``text=True`` decodes with the locale encoding and raises
        ``UnicodeDecodeError`` — a ``ValueError``, so neither arm of the
        ``(OSError, SubprocessError)`` handler catches it.
        """
        shim = tmp_path / "bin"
        shim.mkdir()
        fake_git = shim / "git"
        fake_git.write_text(
            f"#!{sys.executable}\nimport sys\n\nsys.stdout.buffer.write(b'\\xff\\xfe/hooks\\n')\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        monkeypatch.setenv("PATH", str(shim))

        # The assertion is simply that this returns rather than raising.
        assert install_hooks._hooks_dir() is not None
