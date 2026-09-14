# Project Strategy — Standing Review

> **Status**: 📋 Proposed — an opinion synthesis. It ratifies nothing; acting
> on §6–§8 goes through a [sequencing amendment](methodology/decisions.md#sequencing-amendments).
> **Last reviewed**: 2026-09-11 at `cbeea7bc` (v0.3.16 Phase 1, five of
> eleven PRs merged).
> **Next review**: the v0.4.0 plan opening or 2026-11-30, whichever comes
> first — see [§10](#10-review-cadence-and-log).
> **Supersedes**: the four September 2026 strategy reviews,
> [#895](https://github.com/mkhomutov/Persatrix/pull/895),
> [#896](https://github.com/mkhomutov/Persatrix/pull/896),
> [#897](https://github.com/mkhomutov/Persatrix/pull/897) and
> [#898](https://github.com/mkhomutov/Persatrix/pull/898). They are closed
> unmerged; everything load-bearing in them is carried here or in the
> [evidence companion](project-strategy-evidence.md).

## Summary

Four independent reviews reached one diagnosis: excellent engineering, no
users, and a process that cannot notice the difference. They disagreed about
what to do next. This document settles those disagreements, adds what a fifth
check found, and turns the result into a plan with dates and decision rules.

The decision in one paragraph: **finish v0.3.16 by its own cut clauses, then
run one pre-registered experiment before any new release train opens.** The
experiment tests the project's core claim — that a governed multi-agent
discussion with memory beats the simple alternative on quality per dollar.
The wedge, the licence and the fate of the code a wedge does not need are
decided on its result at the first review, not before.

## 1. What the four reviews established

All four measured the same tree at `f52e7ff6` (2026-09-09). Their agreed
findings, re-verified at `cbeea7bc` on 2026-09-11 (commands in the
[evidence companion](project-strategy-evidence.md)):

| # | Finding | Verified 2026-09-11 |
|---|---------|---------------------|
| F1 | No external use: 4 stars, 0 forks, 0 outside issues or PRs, no downloadable release. | ✔ unchanged; 23 unique visitors in 14 days |
| F2 | The loop is closed: every CI job and the Before-Phase-0 step measure the repository against itself. Nothing can go red because of something a person outside did or did not do. | ✔ 11 jobs, 12 required checks, all internal |
| F3 | The direction reverted: the [2026-06-04 amendment](v0.3.x-sequencing.md#amendment-2026-06-04--re-sequence-the-v03x-tail-for-conversation-realism--usefulness-ahead-of-v040) put adoption first and deferred confidentiality as "invisible to a prospective user"; the [2026-07-25 amendment](v0.3.x-sequencing.md#amendment-2026-07-25--add-v0312-memory-that-travels-cross-channel-experience--accounts) pulled it back on three internal reasons. No adoption step was ever recorded. | ✔ |
| F4 | The process outgrew its team: about 3.5 process commits per feature commit across v0.3.12–v0.3.15; monthly merged PRs fell from 228 (June) to 52 (August); about seven non-implementation PRs per release. | ✔ and continuing ([§3](#3-what-this-synthesis-adds)) |
| F5 | The 500-line cap inverted: files cluster in the last 21 lines under it, and [ISSUE-0143](issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md) records the cap blocking a test. | ✔ 24 files at a cap, 100 within 3 % |
| F6 | The core claim is untested: no eval, test or manual test compares the system against not having it. | ✔ zero hits for ablation, baseline or single-call under `evaluators/` |
| F7 | The wedge is unnamed: the [extension spec](persatrix-extension-spec.md) promises eight domains "without code changes"; the README sells three products. | ✔ |
| F8 | The interop surface is empty: MCP is 24 lines of TODO across both sides of the gRPC boundary; no A2A. | ✔ |
| F9 | The polyglot split is a per-change tax paid by one person: Go, Python, Rust and Svelte; proto pins; sanitizer mirrors; frozen SDKs. | ✔ |

All four also agree on what is worth protecting: the five-axis memory scoping
model and its deterministic egress gate; lease-before-call cost control with a
construction-time cap on autonomy; the offline $0 mode and provider aliases;
the test and review discipline.

## 2. Where they disagreed, and the resolution

| Question | Positions | Resolution |
|----------|-----------|------------|
| Extract the budget lease now ([RFC 0046](rfcs/0046-budget-lease-extraction.md))? | #895: yes, first. #896: no — the pre-call budget-gate market closed between May and September. #898: yes, bundled with the governance core. | **No standalone extraction.** The distinctive part is not "budget before the call" but budgets that understand agents, conversations and a closing reserve. That is a property of the governance core and travels with it. Re-target RFC 0046; do not retire it. |
| The memory-boundary work: stop, ship over MCP, or split? | #895: stop after the v0.3.16 shadow gate. #896: it is the differentiator; ship it over MCP. #897: only the gate half is durable; the 1 500-token allocator is eroded by cheap context. | **Split as #897 says, then stop.** The gate (classification, principal, speaker, audience) stays and is the confidentiality story. No further isolation work after v0.3.16 until a user asks. The allocator is an experiment question, not a bet ([§3](#3-what-this-synthesis-adds)). |
| What is the wedge? | #895, #896: memory and cost control, reachable over MCP. #897: do not commit until measured. #898: discussions that finish, within budget, respecting who is listening. | **#898's framing is the hypothesis; #897's rule governs.** It is the only candidate that uses the largest built asset (20 000 lines of channel governance), has a working demo (the v0.3.11 autonomous channel, $0.17) and is not a commodity. It is tested in [§4](#4-the-decision-framework) before the README changes. |
| Pause v0.4.0, or cap the v0.3.x train? | #898: pause; finish v0.3.16 small. #896: an amendment that adds a release must name what it removes. | **Both.** v0.3.16 closes by its own cut clauses; no train opens before the experiment reports; the "name what you remove" rule goes into the amendment template. |
| Retire the Rust CLI? | #895, #896: retire. #898: four languages is the problem, not one of them. | **Freeze; do not delete yet.** Deleting 11 000 lines is a week nobody has; frozen, it costs one clippy job. Drop it from the quickstart now; remove it when the wedge decision says it is off the path. |
| The licence | All four: reconsider BUSL. | **Decide on a date, not now.** Nobody has been turned away by it because nobody has arrived. Whatever is built to be reached ([§8](#8-implementation-changes)) is MIT from day one; the product licence is decided at the first review with a stated default ([§4](#4-the-decision-framework)). |

## 3. What this synthesis adds

Five checks the reviews did not make, or could not.

1. **The pattern continues in real time.** Between the reviews' baseline and
   this document, 35 commits merged: 21 docs, 12 fixes, 2 CI, no features.
   September so far: 77 merged PRs, 7 of them features. The strategy reviews
   themselves became four PRs and this fifth one. Process is generating
   process.
2. **The cap is a treadmill, not a backlog.** v0.3.16 opened with two
   mandatory splits. After them the near-cap count is unchanged: 24 files
   exactly at a limit, 100 within 3 %. Splitting refills the band because the
   seam is chosen by line count.
3. **The allocator is a cost lever, not only a recall lever.** #897 argues
   that cheap long context erodes the 1 500-token budget. In a single chat it
   does. In a channel every persona re-reads context on every turn, so prompt
   size multiplies by members and turns; the v0.3.11 arc spent about 47 000
   tokens in 99 seconds with the allocator on. Whether the allocator earns
   its share of the memory tree is a quality-per-dollar question, so the
   experiment measures cost, not only recall.
4. **A2A is cheaper than it looks.** [RFC 0043](rfcs/0043-inbound-agent-interop-endpoint.md)
   already drafts a bounded inbound endpoint that lets a foreign agent join a
   channel. That is the interop slice the wedge needs, and it is smaller than
   a full A2A implementation.
5. **The visitors are the author.** Of 23 unique visitors in 14 days, 3 came
   from GitHub referrals and 1 from Google; the 328 unique cloners are CI.
   Any adoption number counted from today starts at zero, which makes the
   outer-loop gate ([§7](#7-methodology-changes)) easy to define honestly.

## 4. The decision framework

**Two readings.** #895 named them: a product meant to find users, or a craft
project where the methodology is the deliverable. The licence,
[RFC 0045](rfcs/0045-open-core-extraction-policy.md) and the
[reserved seams](open-core-reserved-seams.md) commit to the first, so this
document is written under it. If the maintainer chooses the second, only
[§7](#7-methodology-changes) applies. That choice is prior to everything
below and belongs to the maintainer.

**The test.** One experiment, pre-registered here so the outcome cannot be
chosen afterwards. Job: "critique this plan and recommend a decision", 20–30
prompts. Five arms on the same provider and model:

| Arm | Setup |
|-----|-------|
| A | One model call playing every role. |
| B | Persona agents in a channel; salience bid, chair and vote-to-end off, round limit only; memory off. |
| C | Governance on, memory off. |
| D | Governance on, memory on across sessions, the 1 500-token allocator as shipped. |
| D′ | As D with the allocator bypassed: the raw recent transcript in a large cached prefix. |

Scored blind by two people and one LLM judge, on synthesis quality and on
recall of facts planted in earlier sessions (the
[dementia-test](manual-tests/MT-MEMORY-005-dementia-test.md) questions ride
the same arc). Reported as quality per dollar and per minute from the cost
ledger the repository already keeps. The eval harness, offline replay and
lease ledger exist; the missing pieces are a knob for the allocator and a
scorer.

**Decision rules, fixed before the run:**

- If C does not clearly beat A on quality per dollar: stop adding society
  features for this job and reposition on the gate alone — memory that knows
  who may see it — reachable over MCP.
- If C beats A and D does not beat C: the allocator is not the asset. Keep
  the gate, freeze the allocator; the wedge is governed discussion.
- If D beats C and D′ matches D: long context replaces the allocator; same
  disposition.
- If D beats both C and D′: the full stack is justified and the wedge is
  #898's sentence as written.

**Defaults at the first review** ([§10](#10-review-cadence-and-log)): the
licence moves to Apache-2.0 for everything outside the reserved seams unless
a commercial conversation is in progress; the workflow engine (about 3 700
lines, no functional change since June) is frozen and leaves the README
unless the experiment used it.

## 5. Direction: the working hypothesis

Until the experiment reports, the one-sentence direction is:

> **Multi-agent discussions that finish, stay inside a hard budget, and
> respect who is listening.**

First user, by hypothesis: research groups studying how groups of agents
behave, because the licence already allows them and the offline mode and
seeds give them reproducibility. Second: developers whose multi-agent chats
pile on and never finish. The README does not change until the experiment
says which sentence is true.

## 6. Plan for the next ten weeks

In order. Each item is one PR or one live arc unless noted, and names its cut
condition.

1. **Close v0.3.16 small (weeks 1–2).** Ship B1 and B2; A3 flips only on
   its green verdict, as already ruled; C1 and C2 keep their joint cut clause;
   cut D3… (the remaining splits) entirely. No new patch release opens after
   the tag.
2. **Make `MEMORY_BUDGET_TOKENS` configurable (week 2).** A key in
   `config/optimization.yaml` with 1 500 as the default. The experiment needs
   it, and so does anyone on a long-context model.
3. **Build and run the experiment (weeks 3–5).** The arms in
   [§4](#4-the-decision-framework); a committed scoring sheet; the result
   published as `docs/experiments/EXP-001-*.md` with transcripts, whichever
   way it falls.
4. **Ratify the outer-loop gate and the process diet (week 3, in
   parallel).** One sequencing amendment carrying
   [§7](#7-methodology-changes) in full.
5. **Make first contact take five minutes (weeks 5–7).** A Docker image and
   a Go binary attached to every release by CI; one command that runs the
   offline demo; one real transcript and synthesis committed as an example so
   a visitor sees the result before installing. No cut condition: this is the
   cheapest adoption lever in the tree.
6. **Show it to five named people (weeks 6–10).** Dated notes, one file per
   conversation, under `docs/user-research/`. Their count is the first entry
   in the external-evidence section.
7. **One interop slice, chosen by the result (weeks 8–10).** If the wedge is
   governed discussion: RFC 0043's inbound endpoint. If it is the gate: an
   MCP server exposing recall and store with the gate in the path, MIT. Not
   both.
8. **First review (2026-11-30 or the v0.4.0 plan opening).**
   [§10](#10-review-cadence-and-log).

Not on the plan: [RFC 0012](rfcs/0012-protocols-organizations.md)
organizations, [RFC 0028](rfcs/0028-agent-decision-policy-engine.md) decision
engine, RFC 0041, the RFC 0046/0047 extractions, any store migration, any new
language.

## 7. Methodology changes

Written as mechanisms because, as #896 showed, this project executes every
gate and forgets every priority.

1. **An external-evidence section in every sequencing amendment.** Required
   fields: installs by anyone other than the author; issues or PRs from
   anyone else; demos shown, to whom and when; user conversations written up;
   experiment results published. A version whose section is empty may not be
   scoped on internal correctness. Enforced by a check on the newest
   amendment section, not by intention.
2. **Name what you remove.** An amendment that adds a release to a train
   names the release it removes, or the train closes.
3. **A patch release gets one document.** Plan, scope locks and checklist in
   one file under the word cap; release-prep PRs 0–4 fold into the last
   implementation PR and the tag PR; the post-release follow-up is the first
   section of the next plan.
4. **The file cap becomes a warning at 500 and a gate at 800.** The near-cap
   notice stays. Splits happen at real seams when a file is being edited for
   another reason, never as a pre-release sweep.
5. **This document is reviewed on a schedule** ([§10](#10-review-cadence-and-log)).
   Each review is a PR that updates the log, the numbers in the evidence
   companion, and the decision rules if evidence changed them.

## 8. Implementation changes

| Change | Why | When |
|--------|-----|------|
| `MEMORY_BUDGET_TOKENS` read from `config/optimization.yaml` | The experiment's knob; the first thing a long-context user changes. | Week 2 |
| Experiment harness: a per-run switch for governance and memory, plus a scorer | [§4](#4-the-decision-framework) | Weeks 3–5 |
| Release artifacts: image, binary, one-command demo, committed transcript | [§6](#6-plan-for-the-next-ten-weeks) item 5 | Weeks 5–7 |
| Rust CLI frozen and removed from the quickstart | [§2](#2-where-they-disagreed-and-the-resolution) | Week 5, with the artifacts |
| Workflow engine frozen and removed from the README pitch | No functional change since June; on no candidate wedge | First review |
| RFC 0046 re-targeted: the lease ships inside the governance core's extraction, not alone | [§2](#2-where-they-disagreed-and-the-resolution) | First review |
| One interop slice: RFC 0043 or an MCP memory server | [§6](#6-plan-for-the-next-ten-weeks) item 7 | Weeks 8–10 |
| Trust score: evidenced by the experiment, or the schema and decay logic retired | #897: a float rendered as one prompt line; no eval shows it changes behaviour | First review |

## 9. What not to do

- Do not open v0.4.0, or another v0.3.x patch, before the experiment reports.
- Do not extract the budget lease alone.
- Do not add a language, a store, or a kind of release document.
- Do not count progress in releases or documents. Twenty-one releases and no
  users is the clearest signal in all four reviews.
- Do not rewrite the README on a guess. It changes once, on the experiment's
  result.

## 10. Review cadence and log

This is a standing document. It is reviewed:

- at every sequencing amendment — the amendment cites the current review and
  fills the external-evidence section;
- at least every eight weeks, and by 2026-11-30 for the first review;
- whenever an experiment reports or a first outside user appears.

A review re-runs the evidence companion's commands, updates the verification
column in [§1](#1-what-the-four-reviews-established), records any decision
rule that fired, and appends a row below. A review that changes a decision is
ratified by the sequencing amendment that acts on it, not by this file.

| Date | Tree | What changed | Decision rules fired |
|------|------|--------------|----------------------|
| 2026-09-11 | `cbeea7bc` | Written from PRs #895–#898, all closed unmerged. | None; the experiment has not run. |
| 2026-09-12 | `604fab72` | The maintainer chose the product reading. [Amendment 2026-09-12](v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens) ratifies §6 items 1–4, §7 items 1–4 as rulings, and §8's RFC 0046 row; K1 rides v0.3.16 as a cuttable fold-in. | None; the experiment has not run. |

## Related documentation

- [Evidence companion](project-strategy-evidence.md) — every number with its
  command, the four reviews' recommendation ledger, and the perishable market
  claims.
- [v0.3.x sequencing](v0.3.x-sequencing.md) — the decision log F3 reads.
- [Release cycle](methodology/release-cycle.md) and
  [Decisions](methodology/decisions.md) — the process §7 changes.
- [RFC 0045](rfcs/0045-open-core-extraction-policy.md),
  [RFC 0046](rfcs/0046-budget-lease-extraction.md),
  [RFC 0043](rfcs/0043-inbound-agent-interop-endpoint.md) — the RFCs §2 and
  §8 re-target.
- [MT-MEMORY-005](manual-tests/MT-MEMORY-005-dementia-test.md) — the recall
  questions the experiment reuses.
