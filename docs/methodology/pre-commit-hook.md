# The Pre-commit Hook

> **Last updated**: 2026-11-01
> The local copy of the checks, run on every commit. Split out of the
> [automation catalogue](automation-catalogue.md), which lists everything
> else that runs without a human typing the steps.

## What it runs

`python scripts/install_hooks.py [--force]` writes a hook into whatever
directory `git rev-parse --git-path hooks` names (so linked worktrees and
`core.hooksPath` both work). The hook runs `scripts/pre_commit.py`, which is
**version-controlled** and warns when the installed hook has drifted from the
installer. Fourteen steps, target under 10 s:

0. regenerate `FILEMAP.md` and `docs/merged-prs.md`, flip merged PRs' plan
rows, and stage them ·
1. `gofmt -l` on staged Go blobs (CRLF-safe) · 2. `ruff check agents/` ·
3. `cargo fmt --check` · 4. doc links · 5. leaked markup · 6. status markers ·
7. RFC index freshness · 8. ROADMAP status rows · 9. file sizes (`--strict`) ·
10. plan status, on the staged copies (no "PR open" row for a merged PR) · 11. amendment
evidence · 12. methodology conformance. A step that exits 3 could not run in
this checkout (a shallow clone): it is reported as skipped, its file is not
staged, and its reason is repeated after the results.

Because the hook is outside version control it is absent for anyone who did
not run the installer. Since the CI-promotion PR every step has a CI
counterpart: gofmt and cargo fmt in the `Go` / `Rust` jobs, ruff in `Python`,
the doc checks and FILEMAP freshness in `Docs hygiene`, the RFC index, the
ROADMAP status rows and file sizes in `Validate configs` / `File size check`.
The hook is the fast local copy, not the only copy.

## Related documentation

- [automation-catalogue.md](automation-catalogue.md) — every target, script and workflow
- [enforcement-matrix.md](enforcement-matrix.md) — which checks CI requires
