# Project Strategy Review — September 2026

> A standing assessment of the project's premise, direction, and
> implementation strategy, written at the v0.3.16 Phase 1 tip
> (`f52e7ff6`, 2026-09-09). It reviews *choices*, not code correctness —
> the tree builds, vets, and passes its tests. Measurements are in the
> [evidence companion](project-strategy-review-2026-09-evidence.md);
> this file carries the argument.
>
> This is an opinion document, not a decision. Nothing here has been
> ratified into the roadmap or a sequencing amendment.

---

## Summary

Twenty-one releases, five months, ~128 000 lines of production code,
~202 000 lines of tests, 134 000 lines of documentation — and four stars,
zero forks, zero external issues, zero external contributors.

That gap is the review. The engineering quality is high and the process
discipline is unusual; neither is the problem. The problem is that both have
been applied to an inner loop with no outer loop attached. Five months of
excellent execution have not been tested against a single user.

---

## 1. The premise: real, but never narrowed to a wedge

"An agent society engine — personas that persist, remember, and interact
socially" is a legitimate premise with genuine academic lineage and at least
one serious commercial neighbour in the stateful-agent space.

The difficulty is in
[the extension spec](persatrix-extension-spec.md), §E1, which names eight
domains — software teams, business operations, research labs, social science,
education, creative writing, game NPCs, customer service — and then commits
to supporting all of them "without code changes — only configuration."

Those eight are not one market. They are at least three, with incompatible
buyers:

- **Simulation and research.** Real, but small, academic, and low willingness
  to pay. Competes with freely published lab frameworks.
- **Work automation.** Large — but crowded with well-funded incumbents, and
  in this segment persona, mood and trust are a *liability*. Buyers there
  want determinism, replay and audit, not agents with feelings.
- **Companion and persona products.** Consumer, saturated, and served by
  products rather than infrastructure.

A framework aimed at all three serves none well enough to displace an
incumbent in any of them. "Without code changes" reads as an engineering
aspiration standing in for a positioning decision — and five months in, that
decision is still open.

The premise is sound. The refusal to choose a wedge is the problem.

---

## 2. The direction: hardening a boundary with no inhabitants

The last four cycles delivered, in order: epoch/session isolation for memory
tool recalls (v0.3.13); a per-request principal so two authenticated people
do not bleed memory (v0.3.14); speaker attribution on derived memory
(v0.3.15); and audience as a second condition on the egress gate (v0.3.16,
in flight).

That is five-plus weeks of consecutive work on multi-tenant memory
isolation, principal partitioning and confidentiality egress — for a system
with no tenants, no authenticated deployments, and no installation outside
the author's machine. The work is correct. It answers questions no user has
asked, because there are no users, and it hardens a security boundary whose
threat model has no inhabitants.

Set against that, the stub inventory: MCP, A2A, bridges, protocols, mesh and
resilience are all comment-only packages, on both sides of the gRPC
boundary. MCP in particular is the tool-integration standard the surrounding
ecosystem has converged on; every comparable framework speaks it; here it is
seven lines of TODO slotted for v0.4.0.

This is the direction inversion in one sentence: **the interoperability
surface that would let the project meet users where they already are is
unbuilt, while the tenancy layer for users who do not exist is on its fourth
consecutive release.**

---

## 3. The implementation strategy: three costs

### 3.1 The process has begun consuming its own budget

Across v0.3.12 → v0.3.15 the ratio is roughly **3.5 process or documentation
commits per feature commit**. The v0.3.13 cycle shipped nine pull requests,
three of which carried code.

[The release cycle](methodology/release-cycle.md) is well designed and
internally coherent. It is also calibrated for a team shipping to paying
customers: a sequencing amendment, a planning-readiness audit, a master plan
PR, release-prep PRs 0–4, and a post-release follow-up — about seven
non-implementation pull requests per release, executed by one person for an
audience of none.

The cost is visible in throughput: monthly commits fell 247 → 41 and monthly
lines added fell 177 000 → 27 000 across the window, while mean commit size
stayed flat. That is less work landing, not larger batches. Available time
explains part of it; the process grew over exactly the same window, and the
methodology series landed at the throughput floor.

Process that was protecting quality is now spending the budget that produced
it.

### 3.2 The 500-line cap has inverted

Eighty-eight source files sit in the 480–500 line band, against 159 in the
79-line band beneath it — clustering against the ceiling, not a natural tail.
[ISSUE-0143](issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md) already
records the consequence: 26 files at exactly the cap, one of them blocking a
test another issue needs.

Files are increasingly split by line count rather than by cohesion, which is
worse than a long file — it scatters one concept across three modules with
import ceremony between them. The cap has turned from a review aid into a
tax on correctness work.

### 3.3 Polyglot is a tax with no return at this scale

Go, Python, Rust and gRPC, maintained by one person. Every feature crosses at
least two language boundaries, and the costs are enumerable: dual proto
staleness gates against a pinned toolchain, hand-maintained sanitizer enum
mirrors kept in sync by CI, three lint and type stacks, and three open issues
recording toolchains frozen because two languages must move together. CI runs
no Go linter at all — the same shape of gap that once let a red Rust suite
merge green.

The Rust CLI is the clearest case: ~11 000 lines of thin REST client, for a
single-binary distribution the project has no distribution channel for. The
rationale in the spec is textbook-correct for a company with three teams. For
one person it multiplies the cost of every change by the number of boundaries
it crosses.

---

## 4. What is genuinely valuable

Several things here are better than what comparable projects ship:

1. **The memory scoping model.** Session, epoch, room, principal and speaker
   axes, with a deterministic confidentiality egress gate
   ([`injection_gate.py`](../agents/persona_runtime/injection_gate.py)). The
   reasoning in that module records its own non-goals, explains why the
   audience check sits inside the gate rather than in front of it, and pins
   the coverage invariant with a positive-list test. "Memory that crosses
   rooms without leaking, and knows who said what" is a hard, defensible
   asset that the obvious commercial neighbour does not have.
2. **Lease-before-call cost control** ([RFC 0023](rfcs/0023-llm-call-leasing.md)).
   Structural cost gating rather than post-hoc token accounting — a real
   differentiator, and aimed squarely at the failure every agent developer
   has been burned by.
3. **Mandatory caps on autonomy.** Making the cost cap a construction-time
   requirement, so uncapped autonomy is un-creatable, is a good safety
   primitive earned from a real incident.
4. **The provider alias layer and the offline $0 mode.** These remove the two
   scariest onboarding barriers — API key and spend — and are among the best
   product decisions in the repository.

**The right insight has already been written down.**
[RFC 0046](rfcs/0046-budget-lease-extraction.md), dated 2026-05-25, states in
its own motivation that the flagship funnel asset is locked in BUSL and that
what should ship is a library, not a framework — single-language, with
adapters for external agent frameworks. It has been `proposed` and gated
behind `v0.4.0+` for three and a half months.

That is the sharpest finding here. The version-train gate is otherwise the
best rule in the process; in this one case, the thing it is holding back is
the cure for the problem the rest of this document describes.

---

## 5. The two readings

Everything above assumes the project is meant to find users — which the
BUSL licence, the reserved Private tier in
[open-core reserved seams](open-core-reserved-seams.md), and the operational
moat framing all assert.

Under the other reading — a craft project, a vehicle for practising release
engineering, RFC discipline and hard distributed-memory design at a standard
most professionals never reach — the project is succeeding completely, the
methodology apparatus *is* the deliverable, and twenty-one releases into a
vacuum is the point rather than a failure.

The two readings imply opposite next moves. This review is written under the
commercial reading because that is the one the licence and the open-core
documents commit to. Choosing between them is prior to acting on anything
below.

---

## 6. Recommendations, by leverage

1. **Unblock [RFC 0046](rfcs/0046-budget-lease-extraction.md) now.** Break the
   version train for this one item. It is already designed and it is the only
   work in the tree aimed at users who exist. This contradicts the
   version-train rule deliberately, and should be ratified as a sequencing
   amendment rather than smuggled in.
2. **Implement MCP, in both directions.** Consume MCP tools, and expose the
   memory system *as* an MCP server. That single move makes the best asset
   adoptable in minutes without adopting the framework.
3. **Stop the tenancy work after the v0.3.16 shadow gate.** Do not enforce,
   do not extend. Return when a real deployment demands it.
4. **Choose one wedge and rewrite the README around it.** The evidence points
   at *memory and cost control for multi-agent systems*: it is where the
   differentiation actually is, and it does not ask anyone to abandon their
   stack. "Agent society engine" describes what was built, not a reason to
   use it.
5. **Cut the release cycle to plan → implement → tag.** Fold release-prep
   PRs 0–4 and the post-release follow-up into the implementation stream.
6. **Raise the file cap to ~800 lines or retire it.** It is now blocking
   correctness work by the issue tracker's own account.
7. **Retire the Rust CLI** in favour of the existing Go binary. Removes a
   toolchain, a lint stack and a licence-audit surface for no functional loss.
8. **Reconsider BUSL.** It currently costs the project the only thing it has
   none of — users — to protect the only thing it has none of: revenue. A
   permissive licence now, revisited if traction ever arrives, carries no
   practical risk at this scale.

---

## Related documents

- [Evidence companion](project-strategy-review-2026-09-evidence.md) — every
  measurement this review rests on.
- [Roadmap](../ROADMAP.md) — the plan this review assesses.
- [Release cycle](methodology/release-cycle.md) — the process discussed in §3.1.
- [Open-core reserved seams](open-core-reserved-seams.md) /
  [RFC 0045](rfcs/0045-open-core-extraction-policy.md) — the commercial intent
  §5 takes at face value.
