# vX.Y.Z Plan — <Codename> (<one-line story, as a subtitle>)

**Status**: ⬜ Phase 0 — planning PR open
**Target version**: vX.Y.Z (<what kind of release: the <theme> release — one of N before the <next train>>)
**Created**: YYYY-MM-DD
**Branch prefix**: `feature/vXYZ-`
**Target**: `main`
**Merge strategy**: Squash merge per `docs/BRANCHING.md`
**Codename**: *<Codename>*

**Goal**: <Two to four sentences. What holds today, what does not, and what
this release makes true. Name the issues and RFC phases by ID; link each.>

Scope was ratified by `docs/v0.3.x-sequencing.md` §Amendment YYYY-MM-DD,
whose next-steps item <n> this executes. <If an issue-owned PR plan already
exists, say so here: "The per-PR breakdown already exists — `<path>` — so this
is a thin orchestration overlay, owning what that plan does not: …">

> Guidance: the plan owns sequencing, the live arc, and whatever no RFC or
> issue-owned PR plan already owns. It does not restate designs. Every phase
> below cites where the design lives.

## Scope decisions locked at plan opening (YYYY-MM-DD)

<N locks, in `docs/vX.Y.Z-scope-locks.md` if the plan is near the cap;
otherwise inline.> Binding for the cycle; re-opened by an amendment, not by a
PR. In one line each:

- **<Lock 1 title>** — <decision>; consequence: <what it binds>.
- **<Lock 2 title>** — …

**Out of scope** — deferred explicitly so they do not pressure the cut:

- **<Item>** — <where it goes instead and why>.
- **The vX.Y.(Z+1) bundle** — <issue IDs>.
- **<The next train's on-ramp>** — <RFC phases> stay parked.

---

## Acceptance for vX.Y.Z

The release ships when **all** hold:

- **<Issue A> and <Issue B> close together** — <the observable claim>.
- **<Issue C> is closed** — <the regression test that is the gate>.
- **No migration lands after its consumer** — <name each migration with its
  store and PR; say which ships *with* a consumer and why>.
- **<Behaviour that must be byte-identical>** — <and where it is allowed to differ>.
- **The live proof exists** — <the MT ID> on a live provider, with <the
  evidence obligations named verbatim>.
- **The coherence trade is stated, not discovered** — release notes carry
  <the behaviour change and its cost>.
- **The full gate sweep is green live on host** — Go `-race`, `cargo test`,
  the Python suites (incl. the separate `mypy tests/` leg), Vitest, linters,
  `make validate`, eval-replay, licences. <N> store migrations are expected;
  the checklist names them.

---

## Master Progress Overview

| # | Workstream | Owner | Status |
|---|-----------|-------|--------|
| A | <name> — <what it delivers> | <PR plan path> PRs 1–n | ⬜ |
| B | <name> *(cuttable — cut clause: <condition>)* | PR B1 → B2 | ⬜ |
| — | release-prep + tag | `docs/vX.Y.Z-release-prep-plan.md` | ⬜ |

**Legend**: ⬜ · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

> Guidance: reconcile this table at every PR open and every merge. Merged PRs
> leaving their own rows stale is the most common hygiene defect.

---

## Dependency graph

```
vX.Y.(Z-1) (released YYYY-MM-DD)  +  <prior gate or plan>
   │
   └── Phase 0: this planning PR
           ├── A: PR 1 → PR 2 → PR 3 ──┐
           └── B1 → B2 ────────────────┤
                                       ▼
                     Phase 2: release-prep plan (PR 0)
                        → Phase 3: release-prep PRs 1–4, live arc in PR 1
                        → Phase 4: tag + follow-up
```

Hard edges: <list each, with the reason it is not optional>.

---

## Phase 0 — This planning PR

This doc; ROADMAP hygiene (below); plan-opening notes on <issue IDs>; <any
inherited plan amended>; `FILEMAP.md` regen.

## Phase 1 — Implementation PRs

### Workstream A — <name>

Design and per-PR detail: `<path to RFC / issue-owned PR plan>`. This plan owns
only <what>.

#### PR A1 — `feature/vXYZ-<slug>`
**Scope**: <files/modules>. **Tests**: <the failing test that goes first>.
**Acceptance**: <one observable line>. **Migration**: <none | store vN → vN+1, ahead of its reader>.

#### PR A2 — …

### Workstream B — <name> *(cuttable)*

<As above. State the cut clause and what is re-filed if cut.>

## Phase 2 — vX.Y.Z release-prep plan

Open `docs/vX.Y.Z-release-prep-plan.md` from `docs/templates/RELEASE_PREP_PLAN_TEMPLATE.md`:
MT-exec PR → docs/checklist PR → version bump + curated `[X.Y.Z]` CHANGELOG
PR → final verification → tag. Release-notes obligations fixed now: <list>.
Migration expectations: <N>, named.

## Phase 3 — Release-prep execution

The live deliverable is `<MT ID>` on a live provider: <the legs and what each
must show>. Machine-paced in one script (`scripts/manual_tests/`).

## Phase 4 — Tag and post-release follow-up

Tag + GitHub Release once Phase 3 is green (body = curated changelog + Upgrade
Notes + Known Gaps + closing evidence). Post-release follow-up PR from
`docs/templates/POST_RELEASE_FOLLOWUP_TEMPLATE.md`: statuses → Released,
ROADMAP repointed at <next ratified version>, issue closures reflected, this
plan's allowlist entry (if any) retired.

---

## ROADMAP hygiene

- **This planning PR** → Version-Map row gains the plan link; `Last updated` +
  Current-phase refresh — **concise**.
- **PR 1 opens** → row → 🔄; **PR 1 merges** → seed CHANGELOG `[Unreleased]`.
- **Each issue's closing PR** → `status: resolved` + note; `make issues`.
- **Phase 4** → row → ✅ Released; header lines → next version.

## Risk and mitigations

| Risk | Mitigation |
|------|------------|
| <what could go wrong> | <the test, MT leg, lock, or stated Known Gap that bounds it> |

## Decision / next steps

**Status**: ⬜ Phase 0 open.

1. Land this planning PR.
2. Open PR A1 …
3. …

## Related documentation

- `docs/methodology/release-cycle.md` — the cycle this plan instantiates
- `docs/v0.3.x-sequencing.md` §Amendment YYYY-MM-DD — the ratifying decision
- <RFCs, issues, prior plan>
