# Process Glossary

> **Last updated**: 2026-09-06
> Terms used to run the project. Product and architecture terms live in the
> [AI glossary](../ai-glossary.md); this file covers the process vocabulary
> that appears in plans, PR bodies, and reports. Same authoring rules: use the
> canonical term, define a new one here in the same change.

### Sequencing amendment
A dated section appended to [v0.3.x-sequencing.md](../v0.3.x-sequencing.md)
that assigns issues and RFC phases to concrete versions and names the next
version. The citation for "why is this in scope". Never edited; superseded by
a later amendment. See [decisions.md](decisions.md#sequencing-amendments).

### Version-train gate
The rule that work slotted for a later version does not merge while the
current release is uncut, even when requested — the sequencing is flagged
instead.

### Planning-readiness audit
A PR before Phase 0 that gives every issue the amendment slots a dated note
recording its slotting and any plan-opening default, so the plan opens with no
dangling questions.

### Master plan
`docs/vX.Y.Z-plan.md`. The version's orchestration overlay: locks, acceptance,
progress table, dependency graph, Phases 0–4, risks. See
[release-cycle.md](release-cycle.md#phase-0--the-master-plan-pr).

### Codename
The version's short name ("Who said what", "Memory that travels"). Used in the
tag message and release title.

### One-line story
The sentence stating what a developer can do after the release that they could
not before. Every version has one; it is the test of whether the version is a
release or a batch.

### Scope lock
A decision fixed at plan opening with its binding consequence. Re-opened only
by amendment. Lives in the plan or, once the plan nears the cap, in
`docs/vX.Y.Z-scope-locks.md`. See [decisions.md](decisions.md#scope-locks).

### Plan-opening default
The answer a lock assumes where the true answer is not yet known, together
with the PR that will confirm or overturn it.

### Cuttable
A workstream the release will ship without rather than slip for. Marked
*(cuttable)* in the plan; its **cut clause** — the condition for dropping
it — is stated in the amendment. See
[decisions.md](decisions.md#cuttable-items-and-cut-clauses).

### Taken, not cut
The explicit record that a cuttable item shipped.

### Fold-in
A small item taken into a release late because its fix is on the critical path
anyway. Always cuttable, always named in the amendment or the locks.

### Amendment
The only way a ratified decision changes: a dated, appended section (or
file) stating the driver, the before/after, and the ratification PR. See
[decisions.md](decisions.md#amendments).

### Workstream
A group of PRs in the master plan with one owner and one row in the Master
Progress Overview (A, B, C …).

### Issue-owned PR plan
`docs/issues/ISSUE-NNNN-…-pr-plan.md`: the PR breakdown for residual work large
enough to need its own sequencing. The master plan links it instead of
duplicating it.

### Release-prep plan
`docs/vX.Y.Z-release-prep-plan.md`, landed as **release-prep PR 0**. Owns the
sequencing of release-prep PRs 1–4 and the release gate's evidence
obligations. See [release-cycle.md](release-cycle.md#phase-2--the-release-prep-plan-release-prep-pr-0).

### Release baseline
`docs/vX.Y.Z-release-baseline.md`: the "current state" facts split out of the
release-prep plan when it nears the cap — and the list of facts that differ
from the previous release's checklist.

### Release-prep PRs 1–4
The fixed sequence: **PR 1** live arc + execution report; **PR 2** docs
verification + release checklist; **PR 3** version bump + changelog curation;
**PR 4** final pre-tag verification. Then the tag.

### Live arc
The designated manual-test sequence run once, live, on a paid provider, as
the release gate. Machine-paced in one script.

### Leg
One numbered step of a manual test that carries its own evidence and
verdict (Leg 0 … Leg 9).

### Evidence obligation
For each claim the release makes, the artifact that proves it — recorded
verbatim in the execution report — and the reason a green leg without that
artifact is not proof.

### Vacuous
A leg or test that passed without exercising the contested surface (an
absence bar met by an empty read, a fan-out that was suppressed, spans the
sampler dropped). Not an outcome; re-run once the preflight condition holds.

### Absence bar
A check that passes when something is *not* observed. Satisfied by any empty
read, so it always needs a positive control alongside it.

### Preflight
The checks run before a paid arc to prove each leg *can* be answered. Gates
have three states — pass, fail, **skipped** ("cannot be answered yet" is not
a failure). `scripts/manual_tests/`.

### Execution report
`docs/manual-tests/vX.Y.Z-execution-report.md`: the frozen record of the live
arc — environment, run knobs, per-leg results, evidence, findings, issue
dispositions, sign-off. Excluded from the word cap because it is evidence,
not prose.

### Accepted-with-known-gap
A leg outcome: the mechanism works, a bounded gap remains, a tracked issue
owns it, and the release notes state it.

### Release gate not met
The execution-report outcome when a leg fails. The report merges as-is; fix
PRs follow; a re-execution report reopens Phase 3.

### Finding (F-n / P-n)
A numbered review result with a severity and one of four dispositions:
fixed in-PR, deferred to PR N, filed as an issue, accepted-with-known-gap.
`P-n` marks a finding about a plan or runbook rather than code. See
[review-process.md](review-process.md).

### The paraphrase rule
Local-only files (anything gitignored, notably `docs/pr-reviews/`) are never
referenced from a committed file; findings are paraphrased inline.

### Closeout
The final PR of an RFC or workstream: status flips, plan checklist complete,
divergences recorded, nothing else.

### Release checklist
`docs/vX.Y.Z-release-checklist.md`, landed at release-prep PR 2. Every pre-tag
gate as a command, version alignment, upgrade notes, MT sign-off, tag
procedure, Known Gaps. Frozen after the tag.

### Upgrade notes
The changelog subsection an operator must read before upgrading: migrations
by store and direction, coherence trades, metric-shape changes.

### Known Gap
A bounded limitation the release ships with, named in the release notes and
owned by a tracked issue. "Known Gaps to state, not work to do."

### Coherence trade
A deliberate behaviour change whose cost the release notes state rather than
hide (for example per-speaker fragmentation of group memory).

### Post-release follow-up
The Phase 4 PR: statuses → Released with tag links, ROADMAP repointed,
backfills, and any promise that cannot be kept recorded as "NOT done, and
recorded rather than forced".

### Status hygiene
Verifying, before and after every task, that RFC files, plan rows, issue
notes, and ROADMAP agree. [ROADMAP §How to Update](../../ROADMAP.md#how-to-update-this-file).

### Grandfathered
A file on the size allowlist (`scripts/checks/file_size_allowlist.py`) with an
inline reason and an exit condition. Master plans are grandfathered for their
open cycle only.

### Split, don't trim
The rule for a document at its word cap: move a stable half to its own file
rather than deleting rationale to make room for a status flip.

### Companion discussion document
A ratified planning document that spawns RFCs but owns no implementation
(memory-quality roadmap, storage-architecture roadmap). Same authoring
discipline as an RFC. [rfcs/README.md](../rfcs/README.md#companion-discussion-documents).
