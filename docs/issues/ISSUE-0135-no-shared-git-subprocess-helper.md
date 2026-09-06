---
id: ISSUE-0135
summary: "Six independent implementations of the `run git, capture stdout, strip, tolerate failure` pattern exist across `scripts/` and `tests/perf/`, each with a different failure vocabulary — one crashes on a missing git binary, one returns None, one returns the string `unknown`, one probes returncode by hand. `scripts/checks/__init__.py` advertises itself as the home for shared cross-platform check utilities but has no git module. The divergence is already load-bearing: `install_hooks` tolerates a missing git while `generate_filemap` does not, so a machine without git gets a clean hook install followed by a traceback from the hook it just installed."
status: open
severity: low
area: scripts
created: 2026-08-22
refs:
  - scripts/checks/__init__.py
  - scripts/generate_filemap.py
  - scripts/pre_commit.py
  - scripts/install_hooks.py
  - scripts/checks/doc_links.py
  - tests/perf/personal_tier_latency.py
---

## Summary

The "shell out to git and cope with failure" pattern is written six times,
six ways. There is no shared helper, in a package whose stated purpose is
shared helpers.

## Context

| Call site | Failure handling |
|---|---|
| `scripts/generate_filemap.py:33` `_get_tracked_files` | `check=True`, no `except` — propagates |
| `scripts/pre_commit.py:45` `_staged_go_files` | `except FileNotFoundError` + manual returncode probe |
| `scripts/pre_commit.py:76` (`git show`) | same shape, third spelling |
| `scripts/checks/doc_links.py:208` `_git_ls_md_files` | `except (FileNotFoundError, CalledProcessError)` → `None` |
| `tests/perf/personal_tier_latency.py:292` `_captured_commit` | `except (OSError, SubprocessError)` → `"unknown"` |
| `scripts/install_hooks.py:47` `_hooks_dir` | `except (OSError, SubprocessError)` → `None` |

`scripts/checks/__init__.py` opens with "Shared utilities for cross-platform
Python check scripts" and already factors out `walking`, `analysis`, and
`patterns` submodules — but nothing for git.

Found during the [PR #840](https://github.com/mkhomutov/Persatrix/pull/840)
review, which added the sixth copy.

## Impact

The inconsistency is not cosmetic. `install_hooks._hooks_dir()` returns
`None` when git is unavailable, so the installer reports a clean failure;
`generate_filemap._get_tracked_files()` uses bare `check=True`, so the
same machine gets a traceback out of the hook that was just installed
successfully. Two scripts in the same directory disagree about whether a
missing git is an expected condition.

The same divergence hides subtler defects. #840 found two in the newest
copy alone: no `timeout`, so a wedged git blocks forever; and `text=True`
without an explicit codec, which raises `UnicodeDecodeError` — a
`ValueError`, escaping an `(OSError, SubprocessError)` handler entirely.
Both were fixed in that one call site. The other five were not audited,
and four of them share at least one of the two shapes.

## Proposed fix / investigation path

Add `scripts/checks/git.py` and re-export from `scripts/checks/__init__.py`
alongside `ensure_utf8_streams` and `walk_files`:

```python
def git_output(
    args: Sequence[str],
    *,
    cwd: Path,
    default: str | None = None,
    timeout: float = 10,
) -> str | None:
    """Run `git *args` in `cwd`. Return stripped stdout, or `default`."""
```

It should carry the two fixes #840 made by hand — explicit
`encoding="utf-8", errors="replace"` rather than `text=True`, and a
`timeout` — so routing a call site through it is strictly a robustness
gain.

Migrate incrementally; the call sites have genuinely different desired
defaults (`None`, `"unknown"`, propagate), which the `default` parameter
covers, but `generate_filemap`'s propagate-on-failure is a deliberate
choice worth preserving rather than flattening. Consider whether it wants
`default=None` plus an explicit raise at the call site instead.

Note that `scripts/` is not linted by CI ([[ISSUE-0134]]), so this
refactor will not be caught by tooling if it introduces import cycles or
unused imports — run ruff over the tree by hand.

## Notes

> 2026-08-22 — captured during the #840 review, which added the sixth
> copy and fixed two latent defects in it that the other five were not
> checked for.
