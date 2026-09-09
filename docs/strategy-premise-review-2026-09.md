# Strategy Review — The Premise Under Test (September 2026)

> A third independent assessment of the project's idea, direction, and
> implementation strategy, measured against `main` at `f52e7ff6`
> (2026-09-09). Its findings were fixed before the two earlier reviews
> ([#895](https://github.com/mkhomutov/Persatrix/pull/895),
> [#896](https://github.com/mkhomutov/Persatrix/pull/896)) were read;
> [§6](#6-where-this-sits-beside-the-other-two-reviews) records where the
> three agree and what this one adds.
>
> This is an opinion document, not a decision. Nothing here is ratified
> into the roadmap or a sequencing amendment.

---

## Summary

The two earlier reviews establish the adoption gap and both conclude the
same way: the memory layer is the defensible asset, so ship it where people
can reach it. This review agrees with the diagnosis and takes one step none
of us took — it asks whether the asset has ever been measured.

It has not. In twenty-one releases, ~129 000 lines of production code and
~202 000 lines of tests, **nothing compares this system against not having
it.** Every gate, eval and manual test asks whether the mechanism behaves as
designed. None asks whether the mechanism beats the obvious alternative.

That matters more than the adoption gap, because a wedge chosen from an
unmeasured asset repeats the mistake at a higher price.

---

## 1. The idea — one strong half, one weak half

"Agent society engine" bundles two claims that need separating, because they
are not equally good.

**The weak half — personas, moods and trust make groups of agents work
better.** This is the pitch in the [README](../README.md) and the eight
domains of [the extension spec](persatrix-extension-spec.md) §E1. It is
also unevidenced here, and the mechanism is thinner than the framing
suggests. Trust is a float. Its entire path to model behaviour is
[`relationship_section.py`](../agents/persona_runtime/relationship_section.py),
which renders a line into the system prompt:

```
Relationship with sam (Human user):
  Trust: 0.62
  Interactions: 14
```

Five releases of relationship plumbing — schema, migrations, decay,
attribution — terminate in two decimal places of text handed to a language
model. Nothing in the repository shows a model behaving differently at
`0.62` than at `0.51`, and no eval asks. The one eval that touches trust,
[`EVAL-MEMORY-001`](../evaluators/eval_sets/EVAL-MEMORY-001.yaml), records in
its own comment that the trust-increase assertion "needs live" work and
settles for `> 0.0`.

**The strong half — memory that crosses rooms without leaking, and knows who
said it.** This is real, specific, and hard to retrofit, exactly as both
earlier reviews argue. It is also not what the README sells.

The premise is not wrong. It is two premises, and the project markets the
weaker one.

## 2. Nothing here has been compared to not having it

The test suite is large and good at what it does. What it does is
conformance: does the allocator admit the right tier, does the gate withhold
above the acting level, does a replay re-derive byte-identically. Those are
the right questions *once you have decided to build this*.

The missing question is the prior one. There is no ablation anywhere in the
tree — no run of [MT-MEMORY-005](manual-tests/MT-MEMORY-005-dementia-test.md)
against a baseline that skips the memory layer entirely and simply puts the
recent transcript in the prompt. Every "baseline" in `docs/`, `evaluators/`
and `tests/` is a *within-system* one: a prior schema version, a prior gate
state, a prior latency number. The single near-miss is
[MT-CHANNEL-GOV-002](manual-tests/MT-CHANNEL-GOV-002.md), which contrasts
floor control against the "concurrent shout" that preceded it — and even
there the contrast is narrated, not run, and it is about turn-taking rather
than memory.

So the load-bearing claim — that this machinery produces a persona that
remembers better than a naive one — has never been tested against the naive
one. It may well be true. It is simply not known, and it has been not-known
for five releases of work that assumes it.

The dementia test itself is a genuinely good outcome bar, and the project
deserves credit for writing it. It is graded pass/fail against a persona
with memory. Run it once against a persona without, and it becomes evidence
instead of a specification.

## 3. The 1 500-token ceiling

Here is the number that should drive the next decision.
[`memory_budget.py`](../agents/persona_runtime/memory_budget.py):

```python
MEMORY_BUDGET_TOKENS: int = 1500
```

A hardcoded module constant — not configurable, not in
[`optimization.yaml`](../config/optimization.yaml). The entire memory
subsystem — 15 785 lines, nineteen forward-only migrations, five scoping
axes, salience scoring, eviction, consolidation — exists to decide which
**1 500 tokens** enter the prompt.

That budget was set against a real 2024-era constraint. It is not obviously
a constraint now. A baseline that drops the last several thousand tokens of
transcript into a cached prefix costs a fraction of the engineering, and on
the dementia test cannot suffer a *recall miss* at all — the failure mode
[MT-MEMORY-005](manual-tests/MT-MEMORY-005-dementia-test.md) §Provenance is
built to diagnose.

This splits the last five releases cleanly, and the split is the most useful
thing in this review:

| Half | What it does | Long context does what to it |
|------|--------------|------------------------------|
| **Allocator** — scoring, eviction, budget, consolidation | picks which 1 500 tokens win | **erodes it.** Cheaper context and prompt caching remove the scarcity it manages |
| **Gate** — classification, protection levels, principal, speaker, audience | decides which memories may be shown *to this room, to this person* | **raises its value.** A bigger prompt makes an unfiltered leak larger, not smaller |

Both earlier reviews recommend doubling down on "memory". Only half of it is
durable. The confidentiality and attribution boundary survives a world of
cheap context; the budget allocator is a solution to a constraint that is
receding. Every hour spent tuning recall scoring is spent on the eroding
half.

## 4. Are we doing the right thing, and is there a future

**Direction:** partly. The work of the last five releases is good, and the
half of it that will still matter in two years is the gate, not the
allocator. The sequencing critique in
[#896](https://github.com/mkhomutov/Persatrix/pull/896) §2 stands.

**Strategy:** no. The process is now the product. Roughly 10 000 words of
plan, prep and checklist per release, seven non-implementation pull requests
per cycle (#895's count), seventy-three manual tests and eleven CI jobs — all of which
measure the repository against itself — carried by one person whose monthly
commits fell from 247 to 42 over the same window the process grew. That is
not a discipline problem; the discipline is exceptional. It is a budget
problem.

**Future:** conditional, and the condition is narrower than either earlier
review states. As an agent society engine competing with LangGraph and
CrewAI: no. As a confidentiality and attribution boundary for agent
memory — reachable without adopting the framework: plausibly yes, **if** the
boundary beats the naive baseline on a test somebody outside this repository
can run.

That "if" is currently unexamined, and it is cheap to close.

## 5. The extraction is cheaper than it looks

One encouraging measurement, because the earlier reviews imply a bigger job
than the code does.

[`agents/memory/`](../agents/memory/) imports nothing from this project. Its
entire external surface is the standard library, `aiosqlite` and
OpenTelemetry — no orchestrator, no gRPC, no `persona_runtime`. The gate and
its scoping helpers add ~2 400 lines with a handful of local imports.

So the asset is ~18 000 lines that already stand alone, out of ~129 000
lines of production code. **About 14 % of the tree is on the path to the
wedge both earlier reviews recommend.** The Go orchestrator, the Rust CLI,
the channel engine and the web console are the other 86 %.

That is the strategic decision nobody has written down. It is not "should we
ship memory over MCP" — it is "what happens to the six-sevenths of the
codebase that a memory product does not need". Answering it deliberately
(keep as the reference application, freeze, or retire piece by piece) is
better than letting the release train answer it by continuing to fund it.

## 6. Where this sits beside the other two reviews

[#895](https://github.com/mkhomutov/Persatrix/pull/895) and
[#896](https://github.com/mkhomutov/Persatrix/pull/896) reach the adoption
gap, the process-to-feature ratio, the size-cap inversion, the polyglot tax
and the closed measurement loop independently. Treat all three as
corroborating; those findings are not restated here.

This review adds three things and dissents on the shared framing of a
fourth:

1. **No ablation exists** ([§2](#2-nothing-here-has-been-compared-to-not-having-it)).
   Both earlier reviews accept the memory layer as the asset. Neither notes
   it has never been measured against the alternative.
2. **The 1 500-token ceiling splits the asset in half**
   ([§3](#3-the-1-500-token-ceiling)). "Double down on memory" is too coarse:
   the gate is durable, the allocator is eroding.
3. **The extraction is ~14 % of the tree, and the other 86 % needs a
   decision** ([§5](#5-the-extraction-is-cheaper-than-it-looks)).
4. **Dissent on the wedge as stated.** Both recommend repositioning on
   memory. That is right in direction and premature in commitment: it is a
   second wedge chosen the same way the first one was — from inside the
   repository, without evidence. Test it before committing a release train
   to it.

## 7. Suggestions

Ordered by leverage. The first is the only one this review would insist on.

1. **Run the ablation. Two weeks, before any repositioning.** Take
   [MT-MEMORY-005](manual-tests/MT-MEMORY-005-dementia-test.md) and run it
   three ways on the same provider and persona: full memory layer; memory
   layer off with the raw recent transcript in the prompt instead; and gate
   on, allocator off. Publish the table whichever way it falls. If the
   baseline wins, that is the most valuable thing this project could learn,
   and it is knowable for the price of one live arc — the repo already runs
   arcs like this every release cycle for a few dollars.
2. **Make `MEMORY_BUDGET_TOKENS` configurable first.** It is a hardcoded
   constant; the ablation needs it as a knob, and so does anyone who wants
   to try this on a long-context model.
3. **Reposition on the gate, not on memory.** "Agent memory that knows who
   may see it" is the durable half of
   [§3](#3-the-1-500-token-ceiling) and is not what any named memory vendor
   sells. Do it after suggestion 1, not before.
4. **Decide the fate of the other 86 %** ([§5](#5-the-extraction-is-cheaper-than-it-looks)).
   Write it into a sequencing amendment as an explicit disposition —
   reference application, frozen, or retired — rather than continuing to
   fund it by default.
5. **Retire the trust score or evidence it.** Either add an eval showing
   behaviour differs across trust bands, or stop carrying the schema,
   migrations and decay logic for a number no test can show does anything.
6. **Adopt #896's external-evidence gate.** Of the eight-plus suggestions
   across the two earlier reviews, that is the one that changes what a plan
   is permitted to say. This review's suggestion 1 is the same idea aimed
   inward: make the plan cite a measurement it did not choose the outcome of.

The suggestions the earlier reviews already make — MCP, the file cap, the
Rust CLI, BUSL, capping the v0.3.x train — are endorsed and not repeated.

---

## Method

Repository figures come from `git ls-files`, `git log`, `wc -l`, and the
repo's own checkers; document word counts use
[`scripts/checks/file_size.py`](../scripts/checks/file_size.py), not `wc -w`.
Production LOC excludes `_test.go`, `agents/tests/` and `tests/`. The claim
that no ablation exists is a negative result from searching `docs/`,
`evaluators/` and `tests/` for baseline, control, ablation and single-agent
comparison language; it is falsified by producing one.

No market claims are made here. The perishability warning in
[#896](https://github.com/mkhomutov/Persatrix/pull/896) §Method is the reason: this review
rests only on measurements that can be re-derived from the checkout.

## Related documentation

- [Project strategy review](https://github.com/mkhomutov/Persatrix/pull/895) — the first review (PR #895; the document lands with it).
- [Market strategy review](https://github.com/mkhomutov/Persatrix/pull/896) — the second (PR #896).
- [Roadmap](../ROADMAP.md) — the plan under assessment.
- [MT-MEMORY-005](manual-tests/MT-MEMORY-005-dementia-test.md) — the outcome bar §7.1 would turn into evidence.
- [Memory quality roadmap](memory-quality-roadmap.md) — where the dementia test was defined.
