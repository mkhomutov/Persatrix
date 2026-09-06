# Decisions — how scope is frozen, changed, and shrunk

> **Last updated**: 2026-09-06
> Companion to [release-cycle.md](release-cycle.md). Terms are defined in the
> [process glossary](process-glossary.md).

A release is only predictable if the questions that could reopen it are
answered before work starts and can be reopened only through a visible door.
Persatrix uses four mechanisms for that: **sequencing amendments** decide what
a version is; **scope locks** decide how it will be built; **cuttable items**
decide what may be dropped and on what condition; **amendments** are the only
way any of the three changes.

---

## Sequencing amendments

**What**: a dated section appended to
[v0.3.x-sequencing.md](../v0.3.x-sequencing.md) that assigns issues and RFC
phases to concrete versions, states each version's one-line story, and names
the next version after it.

**Rules**

1. The original decision and every prior amendment stay **verbatim**. A new
   amendment supersedes by being later, not by editing.
2. The document's "reading order" note at the top points at the **active**
   amendment so a reader who needs only the current table can skip the
   history.
3. Each entry states a **cut condition** for anything cuttable, so a later
   cut can cite the clause rather than argue it.
4. An amendment is ratified by merging its PR. Ratification date is the
   merge date and is written into the amendment header.
5. A ruling made in one amendment (for example "no pull-forward of RFC 0041
   Phase 1 ahead of v0.4.0", 2026-08-02) **holds** until a later amendment
   says otherwise; a later plan cites the ruling rather than re-deciding it.

**Why not a ticket tracker?** The amendment is one page a reviewer can read in
order; the reasoning that made the ordering is next to the ordering.

---

## Scope locks

**What**: the decisions fixed at plan opening (Phase 0) that the whole cycle
builds on. Each lock is one paragraph: **the decision, then its binding
consequence**, then where the evidence lives. The count is whatever the
version needs — five in v0.3.15, more than a dozen in v0.3.14 — and each was
either named by the amendment or forced by the plan-opening audit.

Examples from v0.3.15 ([scope locks](../v0.3.15-scope-locks.md)):

- *Record shape is `(principal, speaker, scope)` — resolved, not re-opened
  here.* Consequence: two named PRs own the reserve re-size and the release
  note about fragmentation.
- *ISSUE-0130(b) is not gated on R-2 — two stores, two migrations.*
  Consequence: the stores are disjoint; "two migrations" is a statement of
  disjointness, not a budget.
- *One live arc, one MT.* Consequence: two named MT edits need owners before
  the paid run, not closeout notes after it.

**Rules**

1. A lock is **binding for the cycle**. It is re-opened by an amendment, never
   by a PR. A PR that finds a lock wrong records the finding and stops.
2. A lock names a **default** where the answer is not yet known, plus the PR
   that will confirm or overturn it ("plan-opening default … revisitable at
   review"). A deviation from the amendment's wording is allowed when the
   lock says so and gives the reason.
3. Locks are the **stable half** of the plan. When the plan nears the word
   cap they move to `docs/vX.Y.Z-scope-locks.md` and the plan links them;
   the moving half (status, progress, next steps) stays in the plan.
4. Every lock carries an **out of scope** counterpart — what is deferred and
   to where — "so it does not pressure the cut".
5. Locks are **citable**. PR bodies reference a lock by name instead of
   re-arguing it.

**Failure path**: a review finding at plan opening that changes a lock is
folded in before the plan merges and the lock text records the finding
(v0.3.14 F-1/F-2, [#818](https://github.com/mkhomutov/Persatrix/pull/818)).
A finding after merge that would change a lock becomes an amendment or an
issue slotted for the next version — the current release ships the locked
shape with the consequence **stated** in its release notes, not silently
mitigated (v0.3.14's activation-day reset is the precedent for
"accepted-and-stated, not mitigated").

---

## Cuttable items and cut clauses

**What**: a workstream the release would rather ship without than slip for.
It is marked *(cuttable)* in the plan's Master Progress Overview and PR list,
and the amendment that slotted it states the **cut clause** — the condition
under which it is dropped.

**Rules**

1. **The cut is a decision, not a default.** A cuttable item is expected to
   ship. It is cut only when its clause fires (for example "cuts if the shape
   grows into registry persistence").
2. **Taken, not cut** is recorded explicitly when a cuttable item ships, so a
   reader of the plan does not have to infer it (RFC 0040 Phase 1 in v0.3.14;
   ISSUE-0125 in v0.3.15).
3. **A cut is never silent.** The row becomes ✂️ Cut; the plan says what was
   cut and cites the clause; anything the cut item was the sole owner of
   (an RFC pointer, an issue note, an MT edit) is **re-filed** in the same
   PR so nothing is orphaned.
4. **A cut does not grow the next release by stealth.** The item goes back to
   the sequencing doc as an open slotting question for the next amendment.
5. **Fold-ins** are the mirror case: a small item taken into a release late
   because its fix is on the critical path anyway. A fold-in is always
   cuttable and always named in the amendment or the plan's locks
   (ISSUE-0116 in v0.3.13).

---

## Amendments

**What**: the only mechanism that changes a ratified decision — a sequencing
entry, a scope lock, an RFC's phased plan, or a plan's own structure.

**Where they live**

| What is being changed | Where the amendment goes |
|---|---|
| Version scope, ordering, next version | New dated section in [v0.3.x-sequencing.md](../v0.3.x-sequencing.md) |
| A master plan's locks or PR list, mid-cycle | `docs/vX.Y.Z-plan-amendment-YYYY-MM-DD.md` (v0.3.1, v0.3.4 precedents) or a dated **§Amendment** section in the plan when it fits under the cap |
| An RFC's design or phase table | A dated **§Amendment** section in the RFC; the RFC's status marker and the ROADMAP RFC Master Index reflect it |
| An issue's disposition | A dated **Notes** entry on the issue (`> YYYY-MM-DD — …`) |

**Rules**

1. **Dated, appended, never edited.** The text being amended stays; the
   amendment says what changes and why. History is readable in one file.
2. **Driver first.** The amendment opens with what forced it — a review
   finding, live evidence, a released-changelog promise — and links it.
3. **A table of before / after** for each concrete change, so the reader can
   check the implementation against it.
4. **Ratification is the merge.** The header records the date and the PR.
5. **Downstream pointers move in the same PR.** ROADMAP rows, the plan's
   Master Progress Overview, and the issues' notes are updated together, so
   no document reads the pre-amendment state after the merge.

---

## Deciding when a release is ready

The plan's **Acceptance** section is the contract. It is written at Phase 0
and not softened later; a release that cannot meet a line either fixes the
gap, cuts the item that owns the line (with its clause), or **states the gap**
in the release notes as a Known Gap with a tracked issue. "Ships when all
hold" means all — the cycle's history has one "release gate not met" report
([#394](https://github.com/mkhomutov/Persatrix/pull/394)) and it was followed
by fixes and a re-execution, not by a softer bar.

Three outcomes exist for a claim at the gate:

| Outcome | Meaning | What the report shows |
|---|---|---|
| **Pass** | The evidence obligation is met | The artifact, verbatim |
| **Accepted-with-known-gap** | The mechanism works; a bounded, named gap remains | The artifact plus a link to the issue that owns the gap |
| **Fail** | The claim does not hold | The evidence, then the fix PR, then a re-run row |

A fourth state, **vacuous**, is not an outcome: it means the leg did not
exercise the surface and is re-run once the preflight condition is met.

---

## Related documentation

- [release-cycle.md](release-cycle.md) — where each mechanism is used
- [review-process.md](review-process.md) — how findings feed amendments and issues
- [v0.3.x-sequencing.md](../v0.3.x-sequencing.md) — the live sequencing record
- [rfcs/README.md §Divergence Tracking](../rfcs/README.md#divergence-tracking) — the RFC-level cousin of an amendment
