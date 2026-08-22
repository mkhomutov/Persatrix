---
id: ISSUE-0136
summary: "The generated pre-commit hook falls back to a `dirname $0/../..` form when `git rev-parse --show-toplevel` yields nothing, but that arithmetic assumes the hook sits at `<root>/.git/hooks/pre-commit` — an assumption #840 invalidated by teaching the installer to honour `core.hooksPath`. With `core.hooksPath=.githooks` the fallback resolves to the repository's *parent*, `cd`s there successfully, and runs the checks against the wrong tree. The deeper question is whether the fallback should exist at all: the installer in the same PR deliberately refuses to guess a path when git cannot answer, and the hook still guesses."
status: open
severity: low
area: scripts
created: 2026-08-22
refs:
  - scripts/install_hooks.py
  - tests/unit/python/test_install_hooks_worktree_safety.py
---

## Summary

The hook's `$0`-derived fallback hard-codes a layout the installer no
longer guarantees, and contradicts the installer's own stated posture of
not guessing when git is silent.

## Context

`HOOK_CONTENT` in `scripts/install_hooks.py` resolves the committing tree
as:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
    REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fi
cd "$REPO_ROOT" || exit 1
```

The `../..` walks up from `<hooks-dir>/pre-commit`, which is correct only
when the hooks directory is `<root>/.git/hooks`.
[PR #840](https://github.com/mkhomutov/Persatrix/pull/840) made the
installer resolve its destination via `git rev-parse --git-path hooks`,
which honours `core.hooksPath` — verified: an absolute override returns
that path verbatim, a relative one (`git config core.hooksPath myhooks`)
returns the bare string `myhooks`. So the hook can now legitimately live
at `<root>/.githooks/pre-commit`, where `../..` is the parent of the
repository.

`cd` to that parent succeeds — it exists — so the failure is silent
rather than loud: the hook proceeds to run `scripts/pre_commit.py`
relative to the wrong directory.

The window is narrow. It requires `git rev-parse --show-toplevel` to
produce nothing *and* a `core.hooksPath` override, and the same PR added
a guard that skips cleanly when `scripts/pre_commit.py` is absent — which
is the likely outcome in the parent directory. But "narrow and probably
degrades safely" is a weaker property than the rest of the hook has.

## Impact

Low in isolation. The interest is in the inconsistency it exposes.

`_hooks_dir()` was changed in the same PR to return `None` rather than
guess `REPO_ROOT / ".git" / "hooks"`, on the explicit reasoning that the
obvious guess is wrong precisely in the case the function exists for. The
hook's fallback is the same guess, in the same file, kept for the same
"just in case" reason the installer rejected.

## Proposed fix / investigation path

Two coherent options; the choice is a design call, not a bug fix.

**Delete the fallback.** Match the installer:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
    echo "pre-commit: could not resolve the working tree — is git on PATH?" >&2
    exit 1
fi
```

A pre-commit hook that cannot locate its own repository has nothing
useful to do, and `git rev-parse` failing inside a `git commit` is close
to impossible: git itself just ran. This is the option to prefer.

**Or make it layout-agnostic**, if a belt-and-braces path is wanted —
but note there is no cheap correct arithmetic here, because the hook
cannot know its own repository without asking git, which is the thing
that failed.

Either way, `tests/unit/python/test_install_hooks_worktree_safety.py`
already pins that `$0` must stay inside the `if [ -z "$REPO_ROOT" ]`
guard (`test_dollar_zero_is_guarded_not_merely_later`); deleting the
branch means deleting that assertion, and the end-to-end worktree test
carries the real coverage regardless.

## Notes

> 2026-08-22 — captured during the #840 review. Not fixed there: the PR
> was scoped to the worktree-resolution bug and three review findings,
> and this one is a deliberate design choice rather than a defect to
> patch in passing.
