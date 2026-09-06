# The Release Cycle

> **Last updated**: 2026-09-06
> Describes the cycle every release since v0.3.0 has followed. Evidence: the
> `docs/v0.3.*-plan.md` and `docs/v0.3.*-release-prep-plan.md` files, and the
> commit log — fifteen release-prep plans, fifteen "release-prep PR 1" merges,
> eighteen post-release follow-ups, all in the same shape.

A release is one **version** with one **user-facing story**: "a version is
ready when a developer can do something meaningful they could not do before"
([sequencing doc](../v0.3.x-sequencing.md#per-version-user-facing-story)).
Everything below exists to ship that story with evidence and without surprise.

```
Sequencing amendment  ──► Planning-readiness audit
   │
   ▼
Phase 0  Master plan PR            docs/vX.Y.Z-plan.md (+ scope locks)
Phase 1  Implementation PRs        one branch prefix, review findings folded in
Phase 2  Release-prep plan         release-prep PR 0
Phase 3  Release-prep PRs 1–4      live arc → docs + checklist → bump + changelog → final verification
         Tag + GitHub Release
Phase 4  Post-release follow-up PR statuses → Released, ROADMAP repointed, backfills
```

Each phase below states its **entry**, **exit**, **artifacts**, and **failure
path**. The vocabulary is defined in the [process glossary](process-glossary.md).

> **Numbering note.** The phase numbers above have been stable since v0.3.8.
> Earlier plans numbered the same steps differently — the release-prep plan
> was "Phase 3" in v0.3.2 and v0.3.4 and "Phase 4" in v0.3.5 — because those
> plans counted their implementation sub-phases separately. Read an older
> plan by its section titles, not its numbers; the steps are the same.

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
changes; the index does not change.

---

## Phase 0 — the master plan PR

**Entry**: amendment ratified; readiness audit merged.
**Exit**: `docs/vX.Y.Z-plan.md` merged; ROADMAP Version-Map row carries the plan link.

The plan is a **thin orchestration overlay**: it owns sequencing, the live
arc, and whatever no RFC or issue-owned PR plan already owns. It does not
restate designs that live elsewhere. Start from
[`VERSION_PLAN_TEMPLATE.md`](../templates/VERSION_PLAN_TEMPLATE.md) (locks:
[`SCOPE_LOCKS_TEMPLATE.md`](../templates/SCOPE_LOCKS_TEMPLATE.md)). Its fixed
sections, in order:

1. **Header** — status, target version, created date, branch prefix
   (`feature/v0315-`), target `main`, squash merge, codename, goal.
2. **Scope decisions locked at plan opening** — the [scope locks](decisions.md#scope-locks),
   each a decision plus its binding consequence, with an explicit **out of
   scope** list "deferred explicitly so they do not pressure the cut".
3. **Acceptance** — "the release ships when **all** hold": one bullet per
   evidenced claim, including the coherence trades the release notes must
   state and the gate sweep that must be green.
4. **Master Progress Overview** — one row per workstream with owner and status
   (⬜ · 🔄 · 🔀 · ✅ · ✂️ Cut); a final row for release-prep + tag.
5. **Dependency graph** — ASCII, with the hard edges called out in prose.
6. **Phase 0 — this planning PR** — what the PR itself changes (this doc,
   ROADMAP hygiene, plan-opening notes on issues, FILEMAP regen).
7. **Phase 1 — implementation PRs** — per workstream, per PR: branch, scope,
   tests, acceptance. Review findings for these PRs are recorded in the PR
   body and, when deferred, as issues — the per-PR findings *table* is the
   RFC PR plan's pattern, not the master plan's
   ([review-process.md](review-process.md#where-findings-are-recorded)).
8. **Phase 2 / 3 / 4** — one paragraph each, naming the release-prep plan,
   the live deliverable, and the tag + follow-up obligations.
9. **ROADMAP hygiene** — which row flips at which event.
10. **Risk and mitigations** — a table; each row names a risk and the
    mechanism (test, MT leg, lock, or stated Known Gap) that bounds it.
11. **Decision / next steps** — a numbered list that is struck through as it
    completes.
12. **Related documentation**.

When the plan nears the 3 000-word cap the **stable half** (scope locks) is
split into `docs/vX.Y.Z-scope-locks.md` so status flips never pay for
themselves by deleting a lock (precedent: v0.3.15).

**Failure path**: a review finding at plan opening that changes a lock is
folded in before merge as a second commit and recorded in the lock's text
(precedent: [#818](https://github.com/mkhomutov/Persatrix/pull/818) F-1/F-2).
After merge, a lock changes only by amendment.

---

## Phase 1 — implementation PRs

**Entry**: plan merged.
**Exit**: every non-cut workstream row in the Master Progress Overview is ✅;
`main` is a usable release-candidate tip.

Rules that hold for every PR:

- **One branch prefix per version** (`feature/v0315-…`), Conventional Commit
  title, squash merge, target under 500 changed lines.
- **Migrations land ahead of their consumer**, in their own PR, never two
  stores in one PR. A repair migration that must ship *with* its consumer is
  named as such in the plan ([v0.3.15 acceptance](../v0.3.15-plan.md#acceptance-for-v0315)).
- **Every PR is reviewed** ([review-process.md](review-process.md)); findings
  are fixed in-PR, deferred to a named follow-up PR, or filed as an issue.
  A finding is never left unrecorded.
- **The plan row flips at PR open and at merge**, and the Master Progress
  Overview is reconciled at every PR open. Merged PRs leaving their own rows
  stale is the most common hygiene defect in the history.
- **Large residual work gets its own PR plan** owned by the issue
  (`docs/issues/ISSUE-NNNN-…-pr-plan.md`); the master plan links it rather
  than duplicating its PR table.
- **RFC work inside a version** follows the RFC sub-cycle in
  [development-workflow.md](../development-workflow.md).

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

---

## Phase 2 — the release-prep plan (release-prep PR 0)

**Entry**: Phase 1 exit.
**Exit**: `docs/vX.Y.Z-release-prep-plan.md` merged.

The release-prep plan owns Phase 3's sequencing. Start from
[`RELEASE_PREP_PLAN_TEMPLATE.md`](../templates/RELEASE_PREP_PLAN_TEMPLATE.md).
Its sections:

- **Header** as in the master plan, plus branch prefix `feature/v0315-release-prep-`
  and a link to the master plan's Phase 2 anchor.
- **Scope** — what PRs 1–4 will do; **out of scope** — new feature work, the
  next version's bundle, and the issues that are "Known Gaps to state, not
  work to do".
- **The release gate** — the live arc, and its **evidence obligations**: for
  each claim the release makes, the artifact that proves it and why a green
  leg without that artifact is not proof.
- **Documentation timing policy** — public docs before the tag; tag links and
  "Released" stamps after it, in Phase 4.
- **Progress Overview** — PRs 0–4, branch, status, GitHub PR, merged SHA.
- **Current state (baseline)** — the facts the PRs act on: issue roll-up,
  version strings, schema/migration state, wire compatibility, manual-test
  state, eval/golden state, changelog state, dependency-notices state. When
  this section would push the plan over the cap it is split into
  `docs/vX.Y.Z-release-baseline.md`. It must name **every fact that differs
  from the previous release's checklist**, so PR 2 does not copy a wrong row
  forward.
- **Track A** (PRs 1–2) and **Track B** (PRs 3–4), each PR with branch,
  scope, acceptance.
- **Status hygiene** and **Related documentation**.

---

## Phase 3 — release-prep PRs 1–4, then the tag

### PR 1 — the live arc and its execution report

**Entry**: PR 0 merged. **Exit**: `docs/manual-tests/vX.Y.Z-execution-report.md`
(from [`EXECUTION_REPORT_TEMPLATE.md`](../templates/EXECUTION_REPORT_TEMPLATE.md))
at ✅ Complete with zero `Fail` and zero `Pending`.

- Run the designated manual-test arc **once**, **live**, on a real (paid)
  provider, **machine-paced in one script** so governance windows (600 s
  end-vote timers, floor-control rounds) never expire while the operator is
  reading — the pacing rules are in the arc's setup document
  ([MT-MEMORY-GROUP-TENANT-001-setup.md](../manual-tests/MT-MEMORY-GROUP-TENANT-001-setup.md))
  and the driver under `scripts/manual_tests/`.
- Run the offline smoke (`make demo-autonomous`, $0) and `make eval-replay`.
- Record every evidence obligation **verbatim** — tables, triples, counts —
  and the cost.
- Preflight the run for **vacuity**: a leg that can pass while exercising
  nothing (an absence bar satisfied by an empty read, a fan-out suppressed by
  a room setting, a sampler that drops the spans) is not run until the
  preflight says it can be answered. `scripts/manual_tests/` holds the
  drivers and three-state (pass / fail / skipped) gates.
- **Findings** are labelled F-1, F-2, … and dispositioned in the same PR:
  red legs become **in-release fix PRs** (precedent: PR 1a
  [#834](https://github.com/mkhomutov/Persatrix/pull/834)), never
  re-deferrals; capture or reasoning misses become `Accepted-with-known-gap`
  rows citing a tracked issue.
- Every scoped issue closes here (`status: resolved`, `closed_pr`,
  `make issues`), citing the report.

**Failure path — release gate not met**: the report merges as-is, titled
"release gate not met" (precedent: v0.3.2
[#394](https://github.com/mkhomutov/Persatrix/pull/394)); fix PRs follow;
a **re-execution** report ("release gate met",
[#397](https://github.com/mkhomutov/Persatrix/pull/397)) reopens Phase 3.
The tag never moves ahead of the evidence.

### PR 2 — documentation verification and the release checklist

**Entry**: PR 1 merged. **Exit**: `docs/vX.Y.Z-release-checklist.md` merged;
README Roadmap row and ROADMAP Version Map read "release prep".

- **Verify**, against shipped behaviour, every guide, RFC section, and diagram
  this release edited. Fix stale spots in this PR.
- Create the checklist with `make release-doc KIND=release-checklist
  VERSION=X.Y.Z CODENAME="…"` (fills the template's placeholders), then
  reconcile it against the previous one **and the baseline's list of
  differing facts**. Sections: §1 pre-release verification (every gate as a
  command), §2 version alignment, §3 changelog with §3.1 upgrade notes, §4
  manual-test sign-off (cites the report), §5 tag + GitHub Release procedure,
  §6 Known Gaps to state in release notes, §7 summary checklist.
- Enumerate the test targets; never let `make test` read as comprehensive
  (Rust is `cargo test`, the console is `make ui-test`, evals are
  `make eval-replay`).

### PR 3 — version bump and changelog curation

**Entry**: PR 2 merged. **Exit**: every version string at X.Y.Z; a dated
`[X.Y.Z]` section in `CHANGELOG.md`; prior sections untouched.

- `make bump-version VERSION=X.Y.Z`, then `cd cli && cargo update --workspace`
  ([version-bump guide](../guides/version-bump.md)).
- Curate `[Unreleased]` into `[X.Y.Z]`: one bullet per shipped story, not one
  per PR; a PR that landed part of a story folds into that story's bullet.
- Write the **Upgrade Notes** whose obligations the plan fixed in Phase 0 —
  migrations by store and direction, coherence trades, metric-shape changes,
  anything an operator must know before upgrading.

### PR 4 — final pre-tag verification

**Entry**: PR 3 merged. **Exit**: every §1 gate green **live on host** on the
post-bump tip; ROADMAP reads `✅ All pre-tag gates green`; release notes drafted.

- Run the full sweep on a clean checkout: `make release-sweep RUN=1
  REPORT=/tmp/sweep.md` runs the checklist §1 list — all four `make test`
  legs, `cargo test`, `make lint`, `make validate`, proto sync, sanitizer
  sync, `make ui` + `make ui-test` + `make ui-html-check`, `make eval-replay`,
  licences, notices (state whether a delta is expected), sizes, doc gates,
  indexes, and the separate `mypy tests/` leg — and prints the results table
  for the report; add the offline Docker smoke with `--include-optional`.
- Do **not** write "Released". The tag does not exist yet.

### Tag and GitHub Release

```bash
git tag -a vX.Y.Z -m "vX.Y.Z — <codename>"
git push origin main --tags
```

(the same commands the [version-bump guide](../guides/version-bump.md) lists;
the guide owns the pre-tag bump steps, this section owns what follows).

Release body = curated changelog + Upgrade Notes + Known Gaps + the closing
evidence quoted from the PR 1 report. Links in the body must be re-rooted to
absolute GitHub URLs; relative doc links do not resolve from a release page.

---

## Phase 4 — post-release follow-up PR

**Entry**: tag pushed and Release published. **Exit**: nothing in the tree
still says the release is pending. PR body from
[`POST_RELEASE_FOLLOWUP_TEMPLATE.md`](../templates/POST_RELEASE_FOLLOWUP_TEMPLATE.md).

- Statuses → **Released** with the tag link: README roadmap row, ROADMAP
  Version Map + `Last updated` + Current phase, the checklist, the prep plan,
  the master plan's release-prep row.
- ROADMAP forward pointer → the **next ratified version**, not the next
  major train, if an amendment has placed a version in between.
- Issue closures reflected; `make issues` / `make rfcs` clean.
- Backfills for anything Phase 3 deliberately left open, and any new issue
  the release surfaced (precedent: ISSUE-0122 at
  [#817](https://github.com/mkhomutov/Persatrix/pull/817)).
- Anything the plan promised for Phase 4 that **cannot** be done is recorded
  in the PR body as "NOT done, and recorded rather than forced", with the
  reason, so the next cycle does not inherit a promise that cannot be kept
  (precedent: the allowlist exit condition at
  [#838](https://github.com/mkhomutov/Persatrix/pull/838)).
- **Read the near-cap list** (`python scripts/checks/file_size.py --near-cap`)
  and decide the **debt sweep** (below).

### The debt sweep

The size caps are a cliff, and a sweep on 2026-08-29 found 29 code files
sitting at exactly 500 lines against a background of ~3.5 per line-count
bucket — the shape trimming-to-fit leaves behind. The RFC-level cycle has a
refactoring assessment ([development-workflow.md §Phase 6](../development-workflow.md#phase-6--refactoring-assessment));
the release cycle had none. Rule: at every post-release follow-up, if the
near-cap list shows **twenty or more files at their cap**, or two releases
have passed since the last sweep, the follow-up files a `debt-sweep` issue
and the next master plan carries a cuttable **Workstream D — debt sweep**
that splits the at-cap files first (pure structural PRs, no behaviour
change, under 500 lines each). Below the threshold, the follow-up records the
count and moves on.

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
- **Word caps apply to plans.** Master plans and release-prep plans are on the
  size allowlist for the open cycle only; split at 485+ lines or ~2 900 words
  rather than trimming a record.
- **Local-only artifacts are never linked** from committed files
  ([review-process.md](review-process.md#the-paraphrase-rule)).
