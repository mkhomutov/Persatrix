# The Review Process

> **Last updated**: 2026-09-06
> Every PR in this repository is reviewed before merge. This document says how,
> because until now the only committed trace of a review was its findings.

## Shape of a review

Reviews are **local and tool-assisted**. The reviewer (a maintainer, usually
with an AI assistant driving `git diff` and the test suites) reads the whole
diff against the plan that scoped it, runs the checks the diff claims, and
writes a report. The report is a working artifact: it lives in
`docs/pr-reviews/`, which is gitignored, and is **never committed or linked**
(see [the paraphrase rule](#the-paraphrase-rule)). GitHub's review UI is not
used; branch protection requires zero approvals, and the audit trail is the
findings recorded in the PR body and the plan.

A review has three inputs and one output:

| Input | Where it comes from |
|---|---|
| The diff | `gh pr diff N` or the branch against `main` |
| The scope | The PR's row in the master plan, RFC PR plan, or issue-owned PR plan, and the acceptance line it claims to meet |
| The gates | The checks the PR's body says it ran; the reviewer re-runs them rather than trusting the claim |

The output is a **findings list**, each finding numbered `F-1`, `F-2`, … in
the order found, with a severity and a disposition.

## What a review covers

In order of weight:

1. **Does the change do what the plan row says, and nothing the locks forbid?**
   Scope creep and lock violations are findings even when the code is good.
2. **Correctness on the contested surface.** For memory and channel work
   that means the scoping and attribution axes; for CI work, that the gate
   would actually fail on the defect it claims to guard.
3. **Evidence.** A test that passes without exercising the surface (an
   absence bar met by an empty read, a guard whose trigger never fires) is a
   finding — "vacuous", not "green".
4. **Cross-language consistency** — proto, Go, Python, Rust, console all
   updated when a contract moves.
5. **Security** — deny-by-default, no `{@html}`, no credential in a log line.
6. **Documentation and status hygiene** — plan rows, ROADMAP, issue notes,
   glossary terms, FILEMAP, all moved together.
7. **Size and shape** — files under the caps, PR under 500 lines or split.

Style comments are made only where they would change a reader's
understanding.

## Severity

| Severity | Meaning | Default disposition |
|---|---|---|
| **Release-blocking** (High) | The release's claim is false or a boundary leaks | Fixed in-PR, or the release does not tag |
| **Medium** | Wrong behaviour on a real path, or a gate that cannot catch what it claims | Fixed in-PR if inside the PR's scope; otherwise a named follow-up PR in the same release |
| **Low** | A defect with a bounded effect, or a stale doc that misleads | Follow-up PR, or an issue slotted by the next amendment |
| **Info** | Worth recording, no action needed now | Noted in the findings table |

`P-n` labels a **prep** finding — something wrong in a plan, checklist, or
runbook rather than in code (v0.3.15 P-1: the cost-capture recipe grepped a
log line that carried no cost).

## The four dispositions

Every finding takes exactly one. None is "ignored".

1. **Fixed in-PR.** Small, in scope, and the fix does not change what the PR
   is. Committed on the same branch before merge; the PR body lists it under
   "Findings, fixed here" with its number.
2. **Deferred to a named follow-up PR** in the same release. The PR plan gains
   a row (or a "From PR N review" subsection) and the finding is tagged
   `→ deferred to PR N`. This is the RFC-level pattern from
   [development-workflow.md Phase 4](../development-workflow.md#phase-4--core-implementation).
3. **Filed as an issue** when it is out of the release's scope or
   cross-cutting. The issue's `refs` names the PR; the finding text in the PR
   body links the issue. Slotting happens at the next amendment.
4. **Accepted-with-known-gap.** The mechanism works, a bounded gap remains,
   and stating it is the right call. Allowed only with a tracked issue and a
   line in the release's Known Gaps.

A fifth wording appears in Phase 4 follow-ups: **"NOT done, and recorded
rather than forced"** — an obligation the plan set that turned out impossible
as written. It is a disposition for plan promises, not for code findings.

## Where findings are recorded

| PR type | Findings go to |
|---|---|
| RFC implementation PR | The RFC's PR plan: a "Review findings" table in the PR's section, and a "From PR N review" subsection in the follow-up PR's section |
| Version-plan or issue-plan PR | The plan's PR section, same table shape |
| Release-prep PR 1 (the live arc) | The execution report's **Findings & follow-ups** section, and the PR body |
| Any PR | The PR body, as `F-n <one line> — <disposition>` |

The table shape, from RFC 0005 onward:

```markdown
| # | Severity | Finding | Action |
|---|----------|---------|--------|
| F-1 | Medium | The lockstep guard parsed the old file after the split, so it read zero knobs while its non-empty assert stayed satisfied | Fixed in-PR (`cargo test` now red on this) |
| F-2 | Low | MT Leg 2b's run rule is confounded by Leg 2 naming the operator | → deferred to PR 2 (re-run on an empty channel) |
```

## The paraphrase rule

**Local-only files are never referenced in any committed file** — docs, code,
comments, tests, commit messages, PR descriptions, issue refs. "Local-only"
means any path `.gitignore` excludes, and `docs/pr-reviews/` in particular.
A finding that needs recording is **paraphrased inline**; the source file is
never named or linked. The rule keeps every committed reference resolvable
from a clean checkout and keeps working notes out of the permanent record.

## What the reviewer re-runs

At minimum, for the language the diff touches:

| Diff touches | Reviewer runs |
|---|---|
| `internal/`, `cmd/` | `go test ./internal/... -race`; `gofmt -l` |
| `agents/`, `tests/` | The named test file(s); `ruff check`; `mypy` on the tree |
| `cli/` | `cargo test`; `cargo clippy -- -D warnings`; `cargo fmt --check` |
| `web/` | `make ui-test`; `make ui-html-check` |
| `proto/` | `make proto-check` |
| `config/`, `schemas/` | `make validate` |
| `docs/` | `python scripts/checks/doc_audit.py`; `python scripts/checks/file_size.py --strict`; `make rfcs-check issues-check` |
| Anything | `python scripts/pre_commit.py` |

Two discipline notes the history paid for:

- **Snapshot before a mutation check.** A mutation test that reverts with
  `git checkout --` throws away uncommitted fixes; save `git diff` first.
- **Re-check ground truth.** Tool output has been wrong or duplicated in this
  environment; confirm with `git` before acting on a surprising result.

## Fixing after review

Fixes land as additional commits on the PR branch (the squash erases them
anyway). The PR body is updated, not appended: the "Findings" section lists
every `F-n` with its final disposition. If a fix changes what the PR is, the
title changes too — the title becomes the commit message on `main`.

## Merging

Squash merge, once every required check is green and every finding has a
disposition. After merge: pull `main`, flip the plan row, and check the
ROADMAP's affected rows before starting the next PR
([ROADMAP §How to Update](../../ROADMAP.md#how-to-update-this-file)).

## Related documentation

- [release-cycle.md](release-cycle.md) — where reviews sit in each phase
- [decisions.md](decisions.md) — how a finding that changes a lock is handled
- [development-workflow.md §Phase 4](../development-workflow.md#phase-4--core-implementation) — the RFC-level findings pattern this generalises
- [issues/README.md](../issues/README.md) — filing a finding as an issue
