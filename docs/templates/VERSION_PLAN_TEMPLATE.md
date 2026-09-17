# vX.Y.Z Plan — <Codename> (<one-line story, as a subtitle>)

**Status**: ⬜ Plan PR open
**Target version**: vX.Y.Z (<the <theme> release — what it closes>)
**Created**: YYYY-MM-DD
**Branch prefix**: `feature/vXYZ-`
**Target**: `main`
**Merge strategy**: Squash merge per `docs/BRANCHING.md`
**Codename**: *<Codename>*

**Goal**: <Two to four sentences. What holds today, what does not, and what
this release makes true. Name the issues and RFC phases by ID; link each.>

Scope was ratified by `docs/v<line>.x-sequencing.md` (the current line's sequencing doc) §Amendment YYYY-MM-DD,
whose next-steps item <n> this executes.

> Guidance: this file is the release's one document — plan, scope locks and
> release checklist — and it stays under the 3 000-word cap (sequencing
> Amendment 2026-09-12, ruling (e)). Designs stay in their RFCs and issues,
> the gate list in `scripts/release/sweep.py`, the evidence in the execution
> report. A release that does not fit is cut or re-scoped by amendment, never
> split into a second file: `make plan-status-check` fails one.

## vX.Y.(Z-1) follow-up

The previous release's post-release follow-up, done by this plan's PR.

- [ ] Tag `vX.Y.(Z-1)` sits on its tag PR's merge commit (or the unblocking fix PR's), and its GitHub Release is published
- [ ] Nothing still says vX.Y.(Z-1) is pending: the README roadmap row, the ROADMAP Version Map row and header, the status line and Progress rows of `docs/vX.Y.(Z-1)-plan.md`, its row in `docs/manual-tests/README.md`
- [ ] Every issue it closed carries `status: resolved` and `closed_pr`; `make issues` and `make rfcs` are clean
- [ ] Issues the release surfaced: <IDs>, each with a dated note and a slot

**NOT done, and recorded rather than forced**: <any promise the previous plan
made that cannot be kept, and why. Delete this line if there is none.>

## Scope locks

Locked at plan opening (YYYY-MM-DD); binding for the cycle, re-opened by an
amendment, never by a PR. One line each:

- **<A decision, stated as a fact>** — consequence: <what it binds, and which PR owns it>. <If it rests on an unknown: **plan-opening default** <the assumed answer>, confirmed or overturned at PR <n>.>
- **<The cuttable item>** — rides as <shape>; cut clause: <condition>; if cut, <what is re-filed where>.
- **One live arc** — `<MT ID>`, <the legs it gains, and who owns each MT edit before the paid run>.

**Out of scope** — deferred explicitly so they do not pressure the cut:

- **<Item>** — <where it goes instead, and why>.

## Acceptance

The release ships when **all** hold:

- **<Issue A> is closed** — <the observable claim, and the regression test that is its gate>.
- **No migration lands after its consumer** — <each migration: store, from → to, PR, reader; or "none">.
- **The live arc passes** — `<MT ID>` on a live provider, the report recording verbatim: <1. the claim — the artifact that proves it, and why a green leg without that artifact is not proof>.
- **<Behaviour that must stay byte-identical>** — <and where it is allowed to differ; the test that pins it>.
- **The coherence trade is stated, not discovered** — the release notes carry <the behaviour change and its cost>.
- **Every gate is green on the tag PR's head** — `make release-sweep RUN=1 OPTIONAL=1`, plus this release's named suites: <list>.

## Progress

| # | PR | Branch | Status |
|---|----|--------|--------|
| A1 | <what it delivers> | `feature/vXYZ-<slug>` | ⬜ |
| B1 | <what it delivers> *(cuttable)* | `feature/vXYZ-<slug>` | ⬜ |
| — | Tag PR — bump, changelog, gate sweep, statuses | `feature/vXYZ-release` | ⬜ |

**Legend**: ⬜ · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

> Guidance: flip a row to 🔀 with its PR link when the PR opens, and to ✅
> when it merges. A merged PR that leaves its own row stale is the most
> common hygiene defect; `make plan-status-check` fails one.

## Implementation PRs

Order: <only the hard edges, each with the reason it is not optional>.

### A1 — <title>

Design: `<RFC section or issue>`. **Scope**: <files or modules>. **Tests**:
<the failing test that goes first>. **Acceptance**: <one observable line>.
**Migration**: <none | store vN → vN+1, ahead of its reader>.

<One subsection per PR. A cuttable PR names its cut clause.>

## Release checklist

**The last implementation PR** — started once every other non-cut row is ✅
and its review findings are fixed — also runs the release gate:

- [ ] `<MT ID>` live: `docs/manual-tests/vX.Y.Z-execution-report.md` ✅ Complete, zero Fail, zero Pending, every obligation above verbatim, the cost recorded, and its row in `docs/manual-tests/README.md`
- [ ] `make demo-autonomous` ($0) and `make eval-replay` recorded in the report
- [ ] The docs this release edited, checked against shipped behaviour: <guides, RFC sections, diagrams>
- [ ] The scoped issues closed, citing the report; `make issues`
- [ ] Everything the gate added — the report, doc fixes, closures, any fix for a red leg — reviewed before merge

**The tag PR** (`feature/vXYZ-release`):

- [ ] `make bump-version VERSION=X.Y.Z` and `cd cli && cargo update --workspace`
- [ ] `make notices` run and its delta committed, or none
- [ ] `[Unreleased]` curated into a dated `[X.Y.Z]`, one bullet per story, with the Upgrade Notes below
- [ ] Every Phase 0 fact the release notes carry — migrations, notices delta, gate set, closures, Known Gaps — re-checked against this PR's head
- [ ] Migration gate in the report's Final Pre-Tag Verification: a `vX.Y.(Z-1)` store opened by this build and the downgrade refused, or zero migrations verified against code
- [ ] `make release-sweep RUN=1 OPTIONAL=1` green on this PR's head, the offline Docker smoke included, its table in the report's Final Pre-Tag Verification
- [ ] Statuses → ✅ Released with the tag link, dated for the day this PR merges: this plan's status line and every merged Progress row, this PR's included (✂️ Cut rows stay; a cuttable item that shipped says *taken, not cut*; the dated changelog takes the plan out of the stale-row check, so nothing else catches a stale row), the README roadmap row, the ROADMAP Version Map row and header (Current phase → the next ratified version), the RFC Master Index row of every RFC phase or amendment this release shipped
- [ ] Any file-size allowlist entry for this plan dropped
- [ ] Release notes drafted: the `[X.Y.Z]` section, the Upgrade Notes, the Known Gaps, the report's closing evidence

**After it merges**: the same day, tag its merge commit and publish the GitHub
Release, then confirm both on the remote and record them on the tag PR
(`docs/methodology/release-cycle.md` §Tag and GitHub Release). If the merge
commit cannot be tagged, the fix PR that unblocks it re-dates the stamps and
takes the tag. The next plan's first section checks both.

### Upgrade Notes

1. **Migrations** — <how many, by store and direction; drop-in, or a downgrade caution>.
2. **<The coherence trade>** — <the behaviour change and its cost>.
3. **<A metric or wire shape change>** — <what dashboards or clients must re-check>.
4. **What stays byte-identical** — <for which modes or configs an upgrade changes nothing>.

### Known Gaps

| Gap | Owner | Bound |
|-----|-------|-------|
| <one line> | ISSUE-NNNN | <what keeps it bounded> |

## Risks

| Risk | Mitigation |
|------|------------|
| <what could go wrong> | <the test, MT leg, lock or stated Known Gap that bounds it> |

## Related documentation

- `docs/methodology/release-cycle.md` — the cycle this plan follows
- `docs/v<line>.x-sequencing.md` (the current line's sequencing doc) §Amendment YYYY-MM-DD — the ratifying decision
- <RFCs, issues, `docs/vX.Y.(Z-1)-plan.md`>
