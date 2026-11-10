# Enforcement Gaps

> **Last updated**: 2026-11-08
> What the [enforcement matrix](enforcement-matrix.md) says the project
> should change. Split out of the matrix, which keeps every rule with its
> check and how hard the check is.

## What the matrix says the project should change

Listed here so the matrix is honest about its own gaps.

1. ~~Make the six advisory CI jobs required.~~ Done: the owner applied the
   set versioned in [`branch-protection.json`](branch-protection.json) (all
   twelve contexts; a test keeps it equal to the CI job list), and
   `make branch-protection-show` reported the live setting equal to the
   file on 2026-09-15. It stays the one step in this list that is run by
   hand, because it needs the owner's admin token.
2. ~~Move the pre-commit-only and make-only checks into CI.~~ Done in the
   CI-promotion PR of the methodology series: gofmt, cargo fmt, ruff + mypy
   on `scripts/` and `evaluators/`, the Go integration tests, the sanitizer
   sync, `prompt_refs`, and the four doc-hygiene checks. `notices-check`
   stays make-only on purpose (see [its row](enforcement-matrix.md#code-and-build)).
3. **Arm the perf gate** by dispatching `perf-baseline-capture.yml` once —
   after [ISSUE-0058](../issues/ISSUE-0058-perf-gate-runner-variance-tolerance.md)
   settles the runner-variance tolerance; a 20 % fixed band on shared runners
   was judged too flaky to arm during a release-prep window (2026-09-06).
4. ~~Decide the PR-size rule.~~ Decided in the BRANCHING rewrite: guidance,
   with the split heuristic code PRs follow; documentation-heavy release
   evidence is the exception and is named as such.
5. ~~Take RFC 0044 Phase 2 (evals in CI) at v0.3.16, not cut.~~ Taken:
   v0.3.16 PR C2 runs `make eval-replay TIER=stable` inside the required
   `Python` job — $0, deterministic, and every release's paid live arc
   becomes a free regression gate for the next once its golden is promoted.
6. **Re-audit of Make targets with no CI step (2026-09-12, ISSUE-0142
   step 4).** Every `*-check`, `lint-*`, `test-*` and `validate` target now
   has a CI step that runs it or its tool directly, with one exception:
   `notices-check` (Make-only on purpose, see [its row](enforcement-matrix.md#code-and-build)).
   `lint-go` closed with PR C1 and `eval-replay` with PR C2 (item 5).
   `eval-drift`, `eval-verdict`, `release-sweep`, `release-doc` and
   `branch-protection-show` are on-demand tools, not gates.

## Related documentation

- [enforcement-matrix.md](enforcement-matrix.md) — every rule, its check and its level
- [automation-catalogue.md](automation-catalogue.md) — where each check lives
- [branch-protection.json](branch-protection.json) — the required contexts
