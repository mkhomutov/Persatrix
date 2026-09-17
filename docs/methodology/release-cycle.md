# The Release Cycle

> **Last updated**: 2026-09-17
> Describes the cycle a patch release follows since ruling (e) of the
> [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens):
> **one document**, the plan. v0.3.0–v0.3.16 ran the same steps across five
> documents and five release-prep PRs; [§Before ruling (e)](#before-ruling-e)
> maps them onto the phases below.

A release is one **version** with one **user-facing story**: "a version is
ready when a developer can do something meaningful they could not do before"
([sequencing doc](../v0.3.x-sequencing.md#per-version-user-facing-story)).
Everything below exists to ship that story with evidence and without surprise.

```
Sequencing amendment  ──► Planning-readiness audit
   │
   ▼
Phase 0  Plan PR               docs/vX.Y.Z-plan.md: the previous release's follow-up,
                               scope locks, acceptance, PRs, release checklist
Phase 1  Implementation PRs    the last one also runs the live arc and checks the docs
Phase 2  Tag PR                version bump, changelog, gate sweep, statuses → Released
         Tag + GitHub Release  on the tag PR's merge commit
```

Each phase below states its **entry**, **exit**, **artifacts**, and **failure
path**. The vocabulary is defined in the [process glossary](process-glossary.md).
Ruling (e) sets this shape for a **patch release**. No minor release has
opened since; the amendment that opens one says whether it keeps the one
document.

---

## Before Phase 0 — deciding what the version is

### Sequencing amendment

The version's scope is ratified in a dated amendment to the sequencing
document ([v0.3.x-sequencing.md](../v0.3.x-sequencing.md)), never by editing
an earlier decision. The amendment names the version, its codename, its
one-line story, the issues and RFC phases it carries, which of them are
**cuttable**, and the next version after it. It is the citation every later
document uses for "why is this in scope".

**Failure path**: a scope question that surfaces mid-cycle does not reopen the
amendment. It becomes an issue slotted for a later version, or a new dated
amendment ([decisions.md](decisions.md#amendments)).

### Planning-readiness audit

Before the plan opens, every issue the amendment slots gets a dated note
recording its slotting and any plan-opening default the plan will rely on
(precedent: [#807](https://github.com/mkhomutov/Persatrix/pull/807)). The goal
is that the plan can open "with no dangling questions". No status or severity
changes; the index does not change — except to file an issue a prior cycle
promised and did not (precedent: the ISSUE-0146 re-file in
[#885](https://github.com/mkhomutov/Persatrix/pull/885)).

---

## Phase 0 — the plan PR

**Entry**: amendment ratified; readiness audit merged.
**Exit**: `docs/vX.Y.Z-plan.md` merged, its follow-up section ticked; ROADMAP
Version-Map row carries the plan link.

The plan is the release's **one document**: plan, scope locks and release
checklist in one file under the 3 000-word cap. It owns sequencing, the
release gate, and whatever no RFC or issue-owned PR plan already owns. It does
not restate designs, the gate list (`scripts/release/sweep.py` owns that) or
evidence (the execution report owns that). Open it with `make release-doc
KIND=plan VERSION=X.Y.Z CODENAME="…"` from
[`VERSION_PLAN_TEMPLATE.md`](../templates/VERSION_PLAN_TEMPLATE.md). Its
sections, in order:

1. **Header** — status, target version, created date, branch prefix
   (`feature/v0316-`), target `main`, squash merge, codename, goal, and the
   amendment step it executes.
2. **The previous release's follow-up** — [below](#the-follow-up-section).
3. **Scope locks** — each a decision plus its binding consequence
   ([decisions.md](decisions.md#scope-locks)), with an explicit **out of
   scope** list "deferred explicitly so they do not pressure the cut".
4. **Acceptance** — "the release ships when **all** hold": one bullet per
   evidenced claim, including the live arc's evidence obligations, the
   coherence trades the release notes must state, and the gate sweep.
5. **Progress** — one row per implementation PR and one for the tag PR
   (⬜ · 🔄 · 🔀 · ✅ · ✂️ Cut).
6. **Implementation PRs** — per PR: branch, scope, tests, acceptance,
   migration; the hard ordering edges in one line. Review findings are
   recorded in the PR body and, when deferred, as issues
   ([review-process.md](review-process.md#where-findings-are-recorded)).
7. **Release checklist** — the boxes the last implementation PR and the tag PR
   tick, the Upgrade Notes, and the Known Gaps.
8. **Risks** — a table; each row names a risk and the mechanism (test, MT
   leg, lock, or stated Known Gap) that bounds it.
9. **Related documentation**.

**A release that does not fit** under the cap is bigger than a patch release:
cut a cuttable item under its clause, or ask for an amendment that splits the
release. It never grows a second file — `make plan-status-check` fails a
scope-locks, plan-amendment, release-prep-plan, release-baseline or
release-checklist file beside an unreleased patch release's plan. A mid-cycle
change to a lock is a dated **§Amendment** section inside the plan
([decisions.md](decisions.md#amendments)).

**Failure path**: a review finding at plan opening that changes a lock is
folded in before merge as a second commit and recorded in the lock's text
(precedent: [#818](https://github.com/mkhomutov/Persatrix/pull/818) F-1/F-2).
After merge, a lock changes only by amendment.

### The follow-up section

The plan's first section is the previous release's post-release follow-up,
done by the plan PR rather than a PR of its own:

- the previous tag sits on its tag PR's merge commit, and its GitHub Release
  is published;
- nothing in the tree still says the previous release is pending — README
  roadmap row, ROADMAP Version Map and header, that plan's status line, the
  execution-report index row;
- issue closures are reflected, and `make issues` / `make rfcs` are clean;
- any new issue the release surfaced is filed with a dated note and a slot
  (precedent: ISSUE-0122 at [#817](https://github.com/mkhomutov/Persatrix/pull/817));
- anything the previous plan promised that **cannot** be done is recorded as
  "NOT done, and recorded rather than forced", with the reason, so this cycle
  does not inherit a promise it cannot keep (precedent: the allowlist exit
  condition at [#838](https://github.com/mkhomutov/Persatrix/pull/838)).

Between a tag and the next plan the tag PR's statuses stand. Under ruling (a)
of the same amendment that gap can be months, which is why the tag PR writes
them rather than leaving them to this section.

---

## Phase 1 — implementation PRs

**Entry**: plan merged.
**Exit**: every non-cut implementation row in Progress is ✅; the execution
report is ✅ Complete; `main` is a usable release-candidate tip.

Rules that hold for every PR:

- **One branch prefix per version** (`feature/v0316-…`), Conventional Commit
  title, squash merge, target under 500 changed lines.
- **Migrations land ahead of their consumer**, in their own PR, never two
  stores in one PR. A repair migration that must ship *with* its consumer is
  named as such in the plan ([v0.3.15 acceptance](../v0.3.15-plan.md#acceptance-for-v0315)).
- **Every PR is reviewed** ([review-process.md](review-process.md)); findings
  are fixed in-PR, deferred to a named follow-up PR, or filed as an issue.
  A finding is never left unrecorded.
- **The plan row flips at PR open and at merge**, and Progress is reconciled
  at every PR open. Merged PRs leaving their own rows stale is the most common
  hygiene defect in the history; `make plan-status-check` fails one.
- **Large residual work gets its own PR plan** owned by the issue
  (`docs/issues/ISSUE-NNNN-…-pr-plan.md`); the plan links it rather than
  duplicating its PR table.
- **RFC work inside a version** follows the RFC sub-cycle in
  [development-workflow.md](../development-workflow.md).

### The last implementation PR — the release gate

The implementation PR that merges last before the tag PR also runs the
release gate, once its review findings are fixed, so the arc runs on the head
that merges:

- Run the designated manual-test arc **once**, **live**, on a real (paid)
  provider, **machine-paced in one script** so governance windows (600 s
  end-vote timers, floor-control rounds) never expire while the operator is
  reading — the pacing rules are in the arc's setup document
  ([MT-MEMORY-GROUP-TENANT-001-setup.md](../manual-tests/MT-MEMORY-GROUP-TENANT-001-setup.md))
  and the driver under `scripts/manual_tests/`.
- Run the offline smoke (`make demo-autonomous`, $0) and `make eval-replay`.
- Record every evidence obligation **verbatim** — tables, triples, counts —
  and the cost in `docs/manual-tests/vX.Y.Z-execution-report.md` (from
  [`EXECUTION_REPORT_TEMPLATE.md`](../templates/EXECUTION_REPORT_TEMPLATE.md)),
  at ✅ Complete with zero `Fail` and zero `Pending`, with its row in the
  [execution-report index](../manual-tests/README.md).
- Preflight the run for **vacuity**: a leg that can pass while exercising
  nothing (an absence bar satisfied by an empty read, a fan-out suppressed by
  a room setting, a sampler that drops the spans) is not run until the
  preflight says it can be answered. `scripts/manual_tests/` holds the
  drivers and three-state (pass / fail / skipped) gates.
- **Findings** are labelled F-1, F-2, … and dispositioned in the same PR: a
  red leg is fixed before the tag, in this PR or an in-release fix PR
  (precedent: PR 1a [#834](https://github.com/mkhomutov/Persatrix/pull/834)),
  never re-deferred; capture or reasoning misses become
  `Accepted-with-known-gap` rows citing a tracked issue.
- **Verify**, against shipped behaviour, every guide, RFC section, and diagram
  this release edited, and fix stale spots here.
- Every scoped issue closes here (`status: resolved`, `closed_pr`,
  `make issues`), citing the report.

A fix that lands after the arc re-runs, in its own PR, the legs its change can
reach, before the tag PR opens.

**Failure paths**:

- A workstream that cannot make the release **is cut**, citing the
  amendment's cut clause, and its row becomes ✂️. A cut is recorded, never
  silent, and re-files anything that would otherwise be orphaned.
- A finding that reveals a design flaw returns to the owning RFC or issue;
  the plan records the return.
- A gate that turns out to have been silently unrun (a test tree with no
  runner, a check nobody calls) is fixed in the PR that found it, with the
  gap explained in a comment where the fix lives (precedents:
  [#848](https://github.com/mkhomutov/Persatrix/pull/848),
  [#813](https://github.com/mkhomutov/Persatrix/pull/813) F-2).
- **Release gate not met**: the report merges as-is, titled "release gate not
  met" (precedent: v0.3.2 [#394](https://github.com/mkhomutov/Persatrix/pull/394));
  fix PRs follow; a **re-execution** report ("release gate met",
  [#397](https://github.com/mkhomutov/Persatrix/pull/397)) comes before the
  tag PR opens. The tag never moves ahead of the evidence.

---

## Phase 2 — the tag PR

**Entry**: every implementation row ✅ or ✂️; the execution report ✅ Complete.
**Exit**: every version string at X.Y.Z; a dated `[X.Y.Z]` section in
`CHANGELOG.md`, prior sections untouched; the sweep green on the PR's head;
statuses read Released.

Branch `feature/vXYZ-release`. In order:

- `make bump-version VERSION=X.Y.Z`, then `cd cli && cargo update --workspace`
  ([version-bump guide](../guides/version-bump.md)).
- Curate `[Unreleased]` into `[X.Y.Z]`: one bullet per shipped story, not one
  per PR; a PR that landed part of a story folds into that story's bullet.
- Write the **Upgrade Notes** whose obligations the plan fixed in Phase 0 —
  migrations by store and direction, coherence trades, metric-shape changes,
  anything an operator must know before upgrading.
- Run the full sweep on a clean checkout of the PR's head: `make release-sweep
  RUN=1 REPORT=/tmp/sweep.md` runs every standing gate — all four `make test`
  legs, `cargo test`, `make lint`, `make validate`, proto sync, sanitizer
  sync, `make ui` + `make ui-test` + `make ui-html-check`, `make eval-replay`,
  licences, notices (state whether a delta is expected), sizes, doc gates,
  indexes, and the separate `mypy tests/` leg — and prints the results table
  for the report's Final Pre-Tag Verification; add the offline Docker smoke
  with `OPTIONAL=1`, and run the plan's named suites by hand.
- Flip the statuses to **✅ Released** with the tag link: the plan's status
  line and every Progress row, this PR's own included; the README roadmap
  row; the ROADMAP Version Map row and header, whose Current phase moves to
  the **next ratified version** (not the next major train, if an amendment
  has placed a version in between). The tag lands on this PR's merge commit
  minutes later; the next plan's follow-up section checks that it did.
- Draft the release notes.

Dating the changelog also freezes the plan: `scripts/checks/released.py`
reads the dated heading, and the size and plan-status checks stop judging the
plan from this PR on. That is why this PR flips every row itself — no check
will catch one left saying "PR open".

**Failure path**: a gate that goes red on the post-bump head is fixed here
when the fix is release engineering, or by a fix PR that re-runs the legs it
reaches (Phase 1); the tag PR then re-runs the sweep. If the tag cannot follow
the merge, the fix PR that unblocks it corrects the Released date.

### Tag and GitHub Release

```bash
git tag -a vX.Y.Z -m "vX.Y.Z — <codename>" "$TAG_PR_MERGE_COMMIT"
git push origin vX.Y.Z
```

(the [version-bump guide](../guides/version-bump.md) owns the bump steps; this
section owns what follows).

Release body = curated changelog + Upgrade Notes + Known Gaps + the closing
evidence quoted from the execution report. Links in the body must be
re-rooted to absolute GitHub URLs; relative doc links do not resolve from a
release page.

---

## Before ruling (e)

v0.3.0–v0.3.16 ran the same steps across more documents and PRs. Their files
stay where they are as release evidence; read them with this map.

| Then | Now |
|------|-----|
| Master plan `docs/vX.Y.Z-plan.md`; scope locks split into `-scope-locks.md` near the cap (v0.3.15, v0.3.16) | The plan, locks inside |
| Plan amendment `docs/vX.Y.Z-plan-amendment-YYYY-MM-DD.md` (v0.3.1, v0.3.4) | A dated §Amendment section in the plan |
| Phase 2: release-prep plan (release-prep PR 0); its current-state facts split into `-release-baseline.md` near the cap | The plan's Acceptance and release checklist, written at Phase 0 |
| Phase 3: release-prep PR 1 (live arc + report), PR 2 (docs check + `-release-checklist.md`) | The last implementation PR |
| Phase 3: release-prep PR 3 (bump + changelog), PR 4 (final sweep) | The tag PR |
| Phase 4: post-release follow-up PR (Released stamps, backfills) | The tag PR's statuses, and the next plan's follow-up section |

The release checklist was copied forward from the previous release, which is
why the baseline had to name every fact that differed. The plan's checklist
is filled from the template instead, so nothing is copied forward.

Phase numbers were stable from v0.3.8 to v0.3.16. Earlier plans numbered the
same steps differently — the release-prep plan was "Phase 3" in v0.3.2 and
v0.3.4 and "Phase 4" in v0.3.5 — because they counted implementation
sub-phases separately. Read an older plan by its section titles.

### The debt sweep

**Retired** by ruling (e) of the
[sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens):
no follow-up reads the near-cap list to schedule splits, and no plan carries
a sweep.

The rule it replaced: when twenty or more files sat exactly at their size
cap, or two releases had passed since the last sweep, the post-release
follow-up filed a `debt-sweep` issue and the next master plan carried a
cuttable Workstream D that split them. v0.3.16 ran it,
and after its two splits 24 files were still at their cap: a seam chosen by
line count refills the pile
([ISSUE-0143](../issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md)).

Now a code file gets a warning over 500 lines and fails only over 800
([documentation-guide §Size Limits](../documentation-guide.md#size-limits)).
A long file is split at a real seam when a change edits it for another
reason, never in a sweep.

---

## Standing rules that cut across phases

- **Version-train gate.** Work slotted for a later version does not merge
  while the current release is uncut, even when asked; flag the sequencing.
- **Status hygiene before and after every task** —
  [ROADMAP §How to Update](../../ROADMAP.md#how-to-update-this-file). The
  ROADMAP header's latest-changes note stays **short**.
- **Every claim is evidenced.** "Green" means the artifact is in the report.
  A test that passed without exercising the contested surface is recorded as
  vacuous and re-run, not counted.
- **The word cap applies to the plan.** It holds its release under 3 000
  words while the cycle is open — no allowlist entry, no split, no trimmed
  record — and the tag PR's dated changelog heading frees it.
- **Local-only artifacts are never linked** from committed files
  ([review-process.md](review-process.md#the-paraphrase-rule)).
