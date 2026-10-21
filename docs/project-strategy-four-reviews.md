# Project Strategy — The Four Reviews

> Companion to the [standing strategy review](project-strategy.md). This file
> holds what the four September 2026 strategy reviews —
> [#895](https://github.com/mkhomutov/Persatrix/pull/895),
> [#896](https://github.com/mkhomutov/Persatrix/pull/896),
> [#897](https://github.com/mkhomutov/Persatrix/pull/897) and
> [#898](https://github.com/mkhomutov/Persatrix/pull/898) — found, how their
> disagreements were settled, and the five checks the synthesis added. A
> strategy review updates [§1](#1-what-the-four-reviews-established)'s
> verification column, as the standing review's
> [§10](project-strategy.md#10-review-cadence-and-log) sets out. The
> [evidence companion](project-strategy-evidence.md) holds the commands behind
> most of the numbers here; F1's outside-issue count, F4's commit ratio and
> per-release PR count, F5's 21-line band and F7's counts are the four
> reviews' own and have no command there yet.
>
> These sections were the standing review's
> [§1](project-strategy.md#1-what-the-four-reviews-established),
> [§2](project-strategy.md#2-where-they-disagreed-and-the-resolution) and
> [§3](project-strategy.md#3-what-this-synthesis-adds), written with it on
> 2026-09-11 at `cbeea7bc`
> ([#936](https://github.com/mkhomutov/Persatrix/pull/936)), so in them "this
> document", "this synthesis", "today" and "this fifth one" mean that review,
> that date and #936. They moved here word for word on 2026-10-20, only their
> links re-pointed, when that file neared its word cap. Like it, they are an
> opinion synthesis that ratifies nothing; what the sequencing amendments have
> since ratified or superseded is in its
> [review log](project-strategy.md#10-review-cadence-and-log).

## 1. What the four reviews established

All four measured the same tree at `f52e7ff6` (2026-09-09). Their agreed
findings, re-verified at `cbeea7bc` on 2026-09-11 and at `04bc24d0` on
2026-10-20, the first review (commands in the
[evidence companion](project-strategy-evidence.md)):

| # | Finding | Verified 2026-09-11 | Re-verified 2026-10-20 |
|---|---------|---------------------|------------------------|
| F1 | No external use: 4 stars, 0 forks, 0 outside issues or PRs, no downloadable release. | ✔ unchanged; 23 unique visitors in 14 days | ✔ unchanged; 4 unique visitors in 14 days; 0 assets on v0.3.16 |
| F2 | The loop is closed: every CI job and the Before-Phase-0 step measure the repository against itself. Nothing can go red because of something a person outside did or did not do. | ✔ 11 jobs, 12 required checks, all internal | ✔ unchanged |
| F3 | The direction reverted: the [2026-06-04 amendment](v0.3.x-sequencing.md#amendment-2026-06-04--re-sequence-the-v03x-tail-for-conversation-realism--usefulness-ahead-of-v040) put adoption first and deferred confidentiality as "invisible to a prospective user"; the [2026-07-25 amendment](v0.3.x-sequencing.md#amendment-2026-07-25--add-v0312-memory-that-travels-cross-channel-experience--accounts) pulled it back on three internal reasons. No adoption step was ever recorded. | ✔ | ✔ no adoption step since either |
| F4 | The process outgrew its team: about 3.5 process commits per feature commit across v0.3.12–v0.3.15; monthly merged PRs fell from 228 (June) to 52 (August); about seven non-implementation PRs per release. | ✔ and continuing ([§3](#3-what-this-synthesis-adds)) | ✔ September 163 merged PRs, 22 features; October to the 20th, 21 and 4 |
| F5 | The 500-line cap inverted: files cluster in the last 21 lines under it, and [ISSUE-0143](issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md) records the cap blocking a test. | ✔ 24 files at a cap, 100 within 3 % | ◐ half holds: since ruling (e) the cap is a warning at 500 and a gate at 800, so it no longer blocks, but 17 code files sit at exactly 500 and 97 in its last 21 lines (23 and 100 at `cbeea7bc`) |
| F6 | The core claim is untested: no eval, test or manual test compares the system against not having it. | ✔ zero hits for ablation, baseline or single-call under `evaluators/` | ✔ until EXP-001 reports: its harness is built, its scored run not started |
| F7 | The wedge is unnamed: the [extension spec](persatrix-extension-spec.md) promises eight domains "without code changes"; the README sells three products. | ✔ | ✔ the README is unchanged until EXP-001 reports (ruling (j)); [ruling (v)](v0.4.x-sequencing-2026-10-21.md#rulings) lets it be rewritten after the strategy rewrite |
| F8 | The interop surface is empty: MCP is 24 lines of TODO across both sides of the gRPC boundary; no A2A. | ✔ | ✔ unchanged |
| F9 | The polyglot split is a per-change tax paid by one person: Go, Python, Rust and Svelte; proto pins; sanitizer mirrors; frozen SDKs. | ✔ | ✔ unchanged |

All four also agree on what is worth protecting: the five-axis memory scoping
model and its deterministic egress gate; lease-before-call cost control with a
construction-time cap on autonomy; the offline $0 mode and provider aliases;
the test and review discipline.

## 2. Where they disagreed, and the resolution

| Question | Positions | Resolution |
|----------|-----------|------------|
| Extract the budget lease now ([RFC 0046](rfcs/0046-budget-lease-extraction.md))? | #895: yes, first. #896: no — the pre-call budget-gate market closed between May and September. #898: yes, bundled with the governance core. | **No standalone extraction.** The distinctive part is not "budget before the call" but budgets that understand agents, conversations and a closing reserve. That is a property of the governance core and travels with it. Re-target RFC 0046; do not retire it. |
| The memory-boundary work: stop, ship over MCP, or split? | #895: stop after the v0.3.16 shadow gate. #896: it is the differentiator; ship it over MCP. #897: only the gate half is durable; the 1 500-token allocator is eroded by cheap context. | **Split as #897 says, then stop.** The gate (classification, principal, speaker, audience) stays and is the confidentiality story. No further isolation work after v0.3.16 until a user asks. The allocator is an experiment question, not a bet ([§3](#3-what-this-synthesis-adds)). |
| What is the wedge? | #895, #896: memory and cost control, reachable over MCP. #897: do not commit until measured. #898: discussions that finish, within budget, respecting who is listening. | **#898's framing is the hypothesis; #897's rule governs.** It is the only candidate that uses the largest built asset (20 000 lines of channel governance), has a working demo (the v0.3.11 autonomous channel, $0.17) and is not a commodity. It is tested in [§4](project-strategy.md#4-the-decision-framework) before the README changes. |
| Pause v0.4.0, or cap the v0.3.x train? | #898: pause; finish v0.3.16 small. #896: an amendment that adds a release must name what it removes. | **Both.** v0.3.16 closes by its own cut clauses; no train opens before the experiment reports; the "name what you remove" rule goes into the amendment template. |
| Retire the Rust CLI? | #895, #896: retire. #898: four languages is the problem, not one of them. | **Freeze; do not delete yet.** Deleting 11 000 lines is a week nobody has; frozen, it costs one clippy job. Drop it from the quickstart now; remove it when the wedge decision says it is off the path. |
| The licence | All four: reconsider BUSL. | **Decide on a date, not now.** Nobody has been turned away by it because nobody has arrived. Whatever is built to be reached ([§8](project-strategy.md#8-implementation-changes)) is MIT from day one; the product licence is decided at the first review with a stated default ([§4](project-strategy.md#4-the-decision-framework)). |

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
   outer-loop gate ([§7](project-strategy.md#7-methodology-changes)) easy
   to define honestly.

## Related documentation

- [Project strategy — standing review](project-strategy.md) — the decision
  framework, plan and review log these sections feed.
- [Evidence companion](project-strategy-evidence.md) — the commands behind
  most numbers here, and the four reviews' recommendation ledger.
- [v0.3.x sequencing](v0.3.x-sequencing.md) — the decision log F3 reads.
- [RFC 0046](rfcs/0046-budget-lease-extraction.md), which §2 re-targets, and
  [RFC 0043](rfcs/0043-inbound-agent-interop-endpoint.md), the interop slice
  §3 points to.
