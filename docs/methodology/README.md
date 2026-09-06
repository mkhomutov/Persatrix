# Persatrix Methodology

> **Last updated**: 2026-09-06

How this project is run, written down so it can be followed, audited, and
reused. Everything here describes practice that already exists in the
repository's history — it is a description, not a proposal. Where the
practice and an older document disagree, the documents here win and the
older document is marked as such.

## Why this set exists

Persatrix has shipped twenty tagged releases in five months using one
repeatable cycle. Until 2026-09-06 that cycle lived only in its instances:
every `docs/vX.Y.Z-plan.md` and `docs/vX.Y.Z-release-prep-plan.md` repeats the
same structure, and the vocabulary that runs it (scope lock, cuttable, live arc,
closeout, fold-in) appeared nowhere as a definition. The canonical workflow
document described the v0.2-era RFC cycle and had not been edited since May.
This set closes that gap so the methodology can be extracted into a
standalone blueprint later.

## Reading order

| # | Document | Answers |
|---|----------|---------|
| 1 | [Release cycle](release-cycle.md) | What happens between "the next version is decided" and "the tag is published", phase by phase, with entry/exit criteria, the PR each phase produces, and what happens when a phase fails. |
| 2 | [Decisions](decisions.md) | How scope is frozen (sequencing amendments, scope locks), how it changes (amendments, never edits), and how it shrinks safely (cuttable items with cut clauses). |
| 3 | [Review process](review-process.md) | How every PR is reviewed, what a finding looks like, the four dispositions a finding can take, and the rule that review reports are never linked from committed files. |
| 4 | [Process glossary](process-glossary.md) | The terms the other three use. |
| 5 | [Testing strategy](testing-strategy.md) | Every test layer, what it proves, where it runs, how to add to it — and the rule that every test tree has a named runner. |
| 6 | [Enforcement matrix](enforcement-matrix.md) | Every rule with its document, its check, and whether the check is required, advisory, or local-only. |
| 7 | [Automation catalogue](automation-catalogue.md) | Every `make` target, script, hook and workflow, and when each runs. |
| 8 | [Templates](../templates/README.md) | One per document kind the cycle produces: version plan, scope locks, amendment, PR plan, release-prep plan, execution report, release checklist, post-release follow-up, manual test. |

## How this relates to the older process documents

| Document | Role now |
|----------|----------|
| [development-workflow.md](../development-workflow.md) | The **RFC-level** sub-cycle (author → PR plan → implement → follow-ups → refactor → diagrams → close). Still accurate for an RFC; it nests inside [release-cycle.md](release-cycle.md) Phase 1. |
| [BRANCHING.md](../BRANCHING.md) | Branch naming, commit convention, PR title rule. Its release-branch and artifact-publishing sections describe a process that was never used; the [release cycle](release-cycle.md) is authoritative on how a release is made. |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | Setup, quality gates, PR checklist. Accurate. |
| [rfcs/README.md](../rfcs/README.md), [issues/README.md](../issues/README.md), [manual-tests/README.md](../manual-tests/README.md) | The three sub-systems the cycle draws on. Accurate and unchanged. |
| [documentation-guide.md](../documentation-guide.md) | Writing style, size caps, ownership map, where documents live and when they freeze. The [enforcement matrix](enforcement-matrix.md) says which of its rules are checked. |
| [ROADMAP.md §How to Update This File](../../ROADMAP.md#how-to-update-this-file) | The status-hygiene recipe. Unchanged. |

## Ground rules that apply to every document here

- Each stays under the 3 000-word cap enforced by `scripts/checks/file_size.py`.
  When one approaches the cap it is split, not trimmed ([why](../documentation-guide.md#size-limits)).
- Plain English, per the [writing style](../documentation-guide.md#writing-style).
- Claims about the repository are checkable: a command, a path, or a PR number.
