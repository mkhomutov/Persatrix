---
id: ISSUE-0036
summary: "scripts/checks/doc_links.py uses ad-hoc glob + filter chain; tighten to `git ls-files '*.md'` for the source of truth"
status: open
severity: low
area: scripts/checks
created: 2026-05-05
refs:
  - scripts/checks/doc_links.py
---

## Summary

[`scripts/checks/doc_links.py`](../../scripts/checks/doc_links.py)
collects markdown files via `repo_root.glob("*.md")` +
`repo_root.glob("*/*.md")` and then filters out matches under `.git/`
and `docs/` to avoid double-counting and spurious failures. The
filter-chain works but is wider than needed and re-implements (badly)
the "tracked files" set that `git ls-files '*.md'` already exposes
authoritatively.

## Context

Captured during the PR #251 deep review (Finding L-3). PR #251 added
the `.git/` exclusion at
[`scripts/checks/doc_links.py:84-89`](../../scripts/checks/doc_links.py#L84-L89)
because PR-body artifacts (`PR_BODY.md`) the contributor wrote into the
repo root for `gh pr create` ended up cached under `.git/` after `git
stash` operations, and the existing glob picked them up. The fix is
defensive and correct, but the underlying glob is the wrong shape: it
walks the working tree (including untracked, gitignored, and
.git-internal files) rather than the set of files actually under
version control.

## Impact

- **Continued band-aid risk**: any future stash / worktree / submodule
  artifact that ends up in a glob-matchable path will reintroduce the
  same class of false positive. The current fix only patches the one
  case observed (`.git/`).
- **Cognitive cost**: two layers of filtering (`docs/` exclusion +
  `.git/` exclusion) make the intent of the script harder to read than
  "check links in the markdown files git is tracking".
- **Worktree gotchas**: contributors using `git worktree add` may
  produce `.git` *files* (not directories) at the worktree root —
  the current `is_dir`-style mental model held by `os.path.normcase`
  prefix-matching may need re-tuning when that case lands.

## Proposed fix / investigation path

Replace the glob-and-filter chain with a single subprocess call:

```python
result = subprocess.run(
    ["git", "ls-files", "-z", "*.md"],
    cwd=repo_root,
    capture_output=True,
    check=True,
    text=False,
)
files = [
    repo_root / Path(name.decode("utf-8"))
    for name in result.stdout.split(b"\0")
    if name
]
```

`git ls-files` reports only tracked files, never `.git/` internals,
respects `.gitignore` automatically, and handles the worktree-as-file
case transparently. `-z`/null-separated output sidesteps newline and
quoting hazards.

Migration steps:

1. Confirm CI environments have `git` available (they already do —
   `make doc-links` runs on `ubuntu-latest` and Windows runners with
   git installed).
2. Add a fallback for the (rare) case where the script runs outside a
   git checkout (e.g. a downstream consumer running `doc_links.py` from
   a tarball). The fallback can keep the current glob behavior with a
   one-line WARN; this preserves the script's defensive posture
   without making the glob path the default.
3. Drop the `.git/` and `docs/` filter blocks once `git ls-files` is
   the source of truth.

Optional follow-up: extend `git ls-files` to a single recursive call
(rglob currently traverses `docs/` separately) — the `*.md` pathspec
already matches recursively when paired with `git ls-files`.

## Notes

> 2026-05-05 — captured during PR #251 deep review (Finding L-3, marked
> "Optional"). Not a merge blocker for #251; tracked here so the
> cleanup is visible rather than buried in a PR-body artifact.
