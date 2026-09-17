# Process Glossary

> **Last updated**: 2026-09-17
> Terms used to run the project. Product and architecture terms live in the
> [AI glossary](../ai-glossary.md); this file covers the process vocabulary
> that appears in plans, PR bodies, and reports. Same authoring rules: use the
> canonical term, define a new one here in the same change.

### Sequencing amendment
A dated section appended to [v0.3.x-sequencing.md](../v0.3.x-sequencing.md)
that assigns issues and RFC phases to concrete versions and names the next
version. The citation for "why is this in scope". Never edited; superseded by
a later amendment. See [decisions.md](decisions.md#sequencing-amendments).

### External evidence section
The section every sequencing amendment opens with from 2026-09-12 on:
installs by anyone other than the author, issues or PRs from anyone else,
demos shown, user conversations written up, experiment results published.
When every row is "none", the amendment may not scope a version on internal
correctness alone. See [decisions.md](decisions.md#sequencing-amendments)
rule 6; `scripts/checks/amendment_evidence.py` fails when a row is missing,
given twice, blank or "TBD", or when an amendment heading is in another form.

### Pre-registered experiment
An experiment whose materials, arms, scoring and decision rules are merged
before any scored run, so nobody can choose the outcome afterwards; the result
is published whichever way it falls. EXP-001 is the first: its
[pre-registration](../experiments/EXP-001-preregistration.md) applies the
rules in [strategy §4](../project-strategy.md#4-the-decision-framework).

### Arm
One of the setups a pre-registered experiment compares, run on the same
materials and the same model as the others. EXP-001 has five, from one model
call (arm A) to a governed discussion with memory (arm D).

### Decision rule
A rule, fixed before an experiment runs, that names what the project does for
each outcome, such as "if C does not clearly beat A, stop adding society
features for this job". It changes only through a strategy review and the
sequencing amendment that ratifies it.

### LLM judge
A model call that scores an experiment's outputs with the same rubric, anchors
and answer keys as the human raters, without knowing which arm produced each
output. It is one rater among several, never the only one.

### Version-train gate
The rule that work slotted for a later version does not merge while the
current release is uncut, even when requested — the sequencing is flagged
instead.

### Planning-readiness audit
A PR before Phase 0 that gives every issue the amendment slots a dated note
recording its slotting and any plan-opening default, so the plan opens with no
dangling questions.

### Version plan
`docs/vX.Y.Z-plan.md`, the release's **one document**: the previous release's
follow-up, scope locks, acceptance, progress, implementation PRs, release
checklist, risks — under the 3 000-word cap. Called the *master plan* until
v0.3.16, when a release-prep plan, a baseline and a checklist sat beside it.
See [release-cycle.md](release-cycle.md#phase-0--the-plan-pr).

### Codename
The version's short name ("Who said what", "Memory that travels"). Used in the
tag message and release title.

### One-line story
The sentence stating what a developer can do after the release that they could
not before. Every version has one; it is the test of whether the version is a
release or a batch.

### Scope lock
A decision fixed at plan opening with its binding consequence. Re-opened only
by amendment. Lives in the version plan (v0.3.15 and v0.3.16 split theirs
into `docs/vX.Y.Z-scope-locks.md`). See [decisions.md](decisions.md#scope-locks).

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
The only way a ratified decision changes: a dated, appended section stating
the driver, the before/after, and the ratification PR (v0.3.1 and v0.3.4 wrote
plan amendments as separate files). See [decisions.md](decisions.md#amendments).

### Workstream
A group of PRs in the version plan with one owner (A, B, C …); each of its PRs
is a row in the plan's Progress table.

### Issue-owned PR plan
`docs/issues/ISSUE-NNNN-…-pr-plan.md`: the PR breakdown for residual work large
enough to need its own sequencing. The version plan links it instead of
duplicating it.

### Last implementation PR
The implementation PR that merges last before the tag PR. It also runs the
live arc, lands the execution report, checks the docs the release edited
against shipped behaviour, and closes the scoped issues. See
[release-cycle.md](release-cycle.md#the-last-implementation-pr--the-release-gate).

### Tag PR
The release's final PR, whose merge commit is tagged: version bump, dated
changelog with Upgrade Notes, the gate sweep on its head, statuses → Released,
release notes drafted. See [release-cycle.md](release-cycle.md#phase-2--the-tag-pr).

### Release-prep PRs 0–4
How v0.3.0–v0.3.16 got from the last implementation PR to the tag: **PR 0**
the release-prep plan (`docs/vX.Y.Z-release-prep-plan.md`, its current-state
facts split into `-release-baseline.md` near the cap); **PR 1** live arc +
execution report; **PR 2** docs verification + release checklist; **PR 3**
version bump + changelog curation; **PR 4** final pre-tag verification.
Retired by ruling (e) of the sequencing Amendment 2026-09-12: PRs 1–2 fold into
the last implementation PR, PRs 3–4 into the tag PR, PR 0 into the version
plan. See [release-cycle.md](release-cycle.md#before-ruling-e).

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
PRs follow; a re-execution report comes before the tag PR opens.

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
The version plan's checklist section: the boxes the last implementation PR
and the tag PR tick, the Upgrade Notes, the Known Gaps. The gate list itself
lives in `scripts/release/sweep.py`. Until v0.3.16 its own file,
`docs/vX.Y.Z-release-checklist.md`, landed at release-prep PR 2.

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
The first section of the next version plan: the previous tag and Release
checked, anything still saying the release is pending fixed, closures
reflected, new issues filed, and any promise that cannot be kept recorded as
"NOT done, and recorded rather than forced". Until v0.3.16 its own PR after
the tag, which also wrote the Released stamps the tag PR writes now.

### Status hygiene
Verifying, before and after every task, that RFC files, plan rows, issue
notes, and ROADMAP agree. [ROADMAP §How to Update](../../ROADMAP.md#how-to-update-this-file).

### Grandfathered
A file on the size allowlist (`scripts/checks/file_size_allowlist.py`) with an
inline reason and an exit condition. A version plan is grandfathered only as
the last resort at the cap, and its tag PR drops the entry. Master plans were
grandfathered for their whole open cycle.

### Split, don't trim
The rule for a document at its word cap: move a stable half to its own file
rather than deleting rationale to make room for a status flip. A version plan
is the exception: a release that does not fit is cut or re-scoped, or as a
last resort allowlisted until its tag PR — not split.

### Companion discussion document
A ratified planning document that spawns RFCs but owns no implementation
(memory-quality roadmap, storage-architecture roadmap). Same authoring
discipline as an RFC. [rfcs/README.md](../rfcs/README.md#companion-discussion-documents).
