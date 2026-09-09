# Strategy Review — Market Position (September 2026)

> A second, independently derived assessment of the project's premise,
> direction, and implementation strategy, measured against `main` at
> `f52e7ff6` (2026-09-09), the v0.3.16 Phase 1 tip. Its findings were fixed
> before the earlier review in
> [PR #895](https://github.com/mkhomutov/Persatrix/pull/895) was read;
> [§7](#7-where-this-differs-from-the-earlier-review) records where the two
> agree and the two points where this one dissents.
>
> This is an opinion document, not a decision. Nothing here is ratified into
> the roadmap or a sequencing amendment.

---

## Summary

The premise is sound, and the asset the project has actually built is better
than the one the README sells. The direction is defensible on merit and
indefensible on sequencing. The implementation strategy has one structural
flaw that explains everything else: **the methodology has no input from
outside the repository.** Every gate measures the project against itself.

Two market windows were open when the open-core policy was written in May
2026. One has since closed, while the plan that would have entered it sat at
`proposed`. The other is open now, the project is unusually well placed for
it, and it cannot be reached — because the thing that would reach it is seven
lines of `TODO`.

---

## 1. The idea — sound, and the asset is better than the pitch

"An agent society engine — personas that persist, remember, and interact
socially" is a legitimate premise with real academic lineage.

But it is not where the differentiation ended up. Persistence, recall, and
persona are table stakes in this market: Mem0 carries 48 000+ stars and a
$24M Series A, and Letta, Zep, and LangMem all ship durable agent memory. A
newcomer comparing Persatrix's persona layer against those has no reason to
choose it.

What the last five releases built is different, and it is genuinely scarce.
Memory here is scoped on five axes — session, epoch, room, principal, and
speaker — and read through a deterministic confidentiality egress gate
([`injection_gate.py`](../agents/persona_runtime/injection_gate.py)) that
withholds any entry whose protection level exceeds the acting room's
classification, before the entry can compete for tokens. Since v0.3.16 that
gate takes audience as a second condition. Derived memory records who said
it, so hearsay is distinguishable from testimony.

None of the named memory vendors positions on that, and it is not cheap to
add late: it is five releases deep and sits on every read path. **That — not
the society — is the defensible asset.**

The problem in the premise is not the premise. It is
[the extension spec](persatrix-extension-spec.md) §E1, which names eight
domains and commits to serving all of them "without code changes". Those are
at least three markets with incompatible buyers, and in the largest of them
persona and trust are a liability rather than a feature. Five months in,
that positioning decision is still open.

---

## 2. The direction — right work, wrong order, and the correction already failed once

Five consecutive releases — v0.3.12 through v0.3.16 — went to memory
boundaries: cross-room confidentiality, per-request principals, speaker
attribution, audience-scoped egress. The work is correct and, per §1,
valuable. The sequencing is the finding, and it is sharper than "the
direction inverted", because **the project already diagnosed this exact
failure and corrected for it, then reverted.**

On 2026-06-04 the
[sequencing amendment](v0.3.x-sequencing.md#amendment-2026-06-04--re-sequence-the-v03x-tail-for-conversation-realism--usefulness-ahead-of-v040)
re-sequenced the v0.3.x tail by **adoption pull**, in its own words: "to
attract users, the multi-persona conversation must read as realistic and be
useful". It explicitly deferred RFC 0037 confidentiality as "invisible to a
prospective user". Three amendments ran on that axis and shipped v0.3.9,
v0.3.10, and v0.3.11.

Then the
[2026-07-25 amendment](v0.3.x-sequencing.md#amendment-2026-07-25--add-v0312-memory-that-travels-cross-channel-experience--accounts)
pulled RFC 0037 back in. Its three stated reasons are that an internal review
found a gap, that the substrate was ready, and that an RFC had slipped twice.
None is an external input. The adoption axis is not overruled — it is simply
absent, and every amendment since has been on the substrate axis.

Nothing measured whether the adoption arc worked. The v0.3.11 amendment
called the autonomous channel "the single best adoption demo the project can
ship"; there is no record of it being shown to anyone.

That matters for what to do next. This was not a lapse of judgment that
better judgment fixes. A list of better priorities was already written down,
was correct, and did not survive one quarter — because nothing in the process
required it to.

---

## 3. The implementation strategy — the loop is closed

CI runs eleven jobs. The documentation-hygiene job alone carries seven steps:
links and anchors, leaked markup, status markers, `FILEMAP.md` freshness,
merged-PR history, plan-row staleness, and conformance to the methodology's
own manifest. Alongside them sit file size, third-party licences, proto
staleness, Docker-ignore hygiene, cost regression, and config validation.

Every one of them measures the repository against itself. **Not one can go
red because of something a person outside the repository did or did not do.**

The same holds one level up. The release cycle's
[Before Phase 0](methodology/release-cycle.md#before-phase-0--deciding-what-the-version-is)
— the step that decides what a version *is* — takes exactly two inputs: a
sequencing amendment the author writes, and a planning-readiness audit over
the author's own issue tracker. There is no point in the cycle where external
evidence can enter.

That is the root cause. Twenty-one releases produced no external signal not
because the work is poor, but because **nothing in the process is capable of
noticing.** The loop is closed, and a closed loop optimises the only thing it
can see: internal consistency. Which it has achieved, to an unusual standard.

The secondary costs are real but better covered elsewhere, so briefly. Across
v0.3.12 → v0.3.15 — three complete cycles, 64 commits — 34 were `docs` and 21
changed code (14 `feat`, 7 `fix`); the v0.3.13 cycle landed thirteen commits
of which three carried code. Eighty-eight `.go` and `.py` files sit in the
twenty-one-line band below the 500-line cap against 159 in the eighty-line
band beneath it, which is clustering rather than a tail, and
[ISSUE-0143](issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md) records one
of them blocking a test another issue needs. And a three-language split pays
its per-change cost out of one person's time.

There is a flip side worth stating plainly, because it determines what the
suggestions in §6 look like. **This project reliably executes anything it
writes down as a gate.** Gates are the one class of intention here that never
slips. So the fix is not to resolve to prioritise users. It is to write the
outer loop down as a gate.

---

## 4. Are we doing the right thing? — one window closed, one open

### 4.1 The budget-lease window has closed

[RFC 0045](rfcs/0045-open-core-extraction-policy.md) §M-1, dated 2026-05-24,
identifies the per-call budget lease as the flagship funnel asset and argues
that "the existing market is mostly after-the-fact dashboards, not pre-call
gates". That was true when it was written.

It is not true now. Tollgate ships as an MIT Rust gateway that prices every
request by token, reserves budget *before* the call, and refuses an
over-budget request with a `402` before it reaches the provider — the same
mechanism, in the same licence class. LiteLLM enforces hard budget caps
across 100+ providers and is the gateway most self-hosted teams already run.
Portkey open-sourced its gateway under Apache 2.0 in March 2026. Bifrost
ships hierarchical budgets natively.

[RFC 0046](rfcs/0046-budget-lease-extraction.md) has been `proposed` since
2026-05-25, gated behind `v0.4.0+`. **The window it was aimed at closed in
roughly four months, while it waited.** Extracting `persatrix-budget` today
means entering a commoditised category against a permissively licensed
incumbent with the identical mechanism and a running start.

### 4.2 The memory window is open, and the entry price is MCP

The memory market's own consensus is that a memory layer locked to one
framework will not be adopted at scale. Mem0 documents integrations across
21 frameworks and platforms. Its local-first layer ships as an **MCP server**
and works today inside Claude Desktop, Cursor, Windsurf, and VS Code.

Persatrix's memory is locked to Persatrix. Reaching it requires Docker, Go,
Python, Rust, and a BUSL grant that does not permit production use.

[`internal/mcp/mcp.go`](../internal/mcp/mcp.go) is seven lines of `TODO`.
[`agents/tools/mcp_bridge.py`](../agents/tools/mcp_bridge.py) is twelve. Both
sides of the gRPC boundary; both arrived in the scaffolding commit and
neither has taken a functional change since, across 891 commits and 21
releases. MCP has its own section in the MVP specification (§5.2) and its own
row in that document's key design decisions.

That is the whole gap in one sentence: **the asset the market does not have
is the one the market cannot reach.**

---

## 5. Does the project have a real future?

Two answers, because they differ.

**As an agent society engine, competing for multi-agent orchestration
mindshare: no.** That race is effectively decided. LangGraph and CrewAI hold
the enterprise and prototyping ends respectively, with millions of monthly
downloads between them, and a project with one contributor and four stars
does not displace them by being better engineered.

**As a memory and confidentiality layer for multi-agent systems, reachable
over MCP: plausibly yes.** The asset is real, the differentiation is specific
and hard to copy, and it does not ask anyone to abandon the framework they
already run. That is a narrow claim and a testable one.

The determining variable is not engineering capacity — five months have
settled that question. It is whether the project will let evidence from
outside the repository change the plan. Section 2 is the reason to doubt it;
section 3 is the reason it is fixable.

---

## 6. Suggestions

Ordered by leverage. Each is written as a mechanism rather than an intention,
because the record in §2 shows that intentions here do not survive a quarter
and gates always do.

1. **Add the one missing gate — an external-evidence input to Before Phase 0.**
   The sequencing-amendment template gains a required section recording what
   happened outside the repository since the last amendment: installs
   attempted by anyone other than the author, issues filed by anyone else,
   MCP sessions served, demos shown to a named person. A version whose
   section is empty may not be scoped on internal correctness. This is the
   only suggestion that survives being ignored, because it changes what a
   plan is permitted to say — and it is the direct fix for §3.

2. **Ship MCP, memory side first.** Expose the memory system *as* an MCP
   server before consuming MCP tools. The first slice is small — recall and
   store over the existing tiers, with the §D egress gate already in the
   path — and it makes the best asset reachable from Claude Desktop, Cursor,
   and VS Code without adopting the framework. This is the single highest
   ratio of reach to effort in the tree.

3. **Reposition on the memory boundary, not the society.** The README sells
   personas; the differentiation is confidentiality and attribution. The
   sentence that sells it already exists in the codebase: memory that crosses
   rooms without leaking, and knows who said it. "Agent society engine"
   describes what was built, not a reason to use it.

4. **Do not extract `persatrix-budget`.** Retire
   [RFC 0046](rfcs/0046-budget-lease-extraction.md) or re-target it, and
   record §4.1 as the reason. Spending the scarcest resource on a
   commoditised primitive is worse than spending it on the substrate work,
   because at least the substrate work compounds into the §1 asset.

5. **Cap the v0.3.x train.** The original 2026-05-10 decision was three patch
   releases before v0.4.0. Eight amendments later it is sixteen, each locally
   justified. Add a standing rule: an amendment that adds a release must name
   what it removes, or the train closes and the remainder moves to v0.4.x.

6. **Reconsider BUSL for anything meant to be reached.** An MCP server nobody
   may run in production is not a funnel. Whatever ships under suggestion 2
   needs a permissive licence at the boundary, or it will be evaluated and
   discarded on the licence line alone.

7. **Raise the file cap to ~800 lines or retire it.** Eighty-eight files in a
   twenty-one-line band under the ceiling is clustering, not a natural tail,
   and the issue tracker already records it blocking correctness work.

8. **Retire the Rust CLI in favour of the Go binary.** One less toolchain,
   lint stack, and licence-audit surface, for no functional loss — the
   specification already describes the CLI as a thin REST client.

---

## 7. Where this differs from the earlier review

[PR #895](https://github.com/mkhomutov/Persatrix/pull/895) reaches the same
diagnosis independently: the adoption gap, the process-to-feature ratio, the
size-cap inversion, the polyglot tax, and the need to pick a wedge. Those
findings are not restated here; treat the two as corroborating.

This review dissents on two of its recommendations, on external evidence it
did not have:

- **Its first recommendation — unblock RFC 0046 now, as "the only work in
  the tree aimed at users who exist" — is no longer right.** The pre-call
  budget-gate window closed between the RFC's authoring and today
  ([§4.1](#41-the-budget-lease-window-has-closed)).

- **Its third — stop the tenancy work — inverts the actual finding.** That
  work is the differentiator ([§1](#1-the-idea--sound-and-the-asset-is-better-than-the-pitch)).
  The error was never that it was built; it is that it was built where nobody
  can reach it. Ship it over MCP rather than stopping it.

And it adds one finding the earlier review does not carry: the correction was
already attempted in June 2026 and silently reverted six weeks later
([§2](#2-the-direction--right-work-wrong-order-and-the-correction-already-failed-once)),
which is why §6 proposes a gate rather than a priority list.

---

## Method

Repository figures come from `git ls-files`, `git log`, and the repo's own
checkers (word counts via `scripts/checks/file_size.py`, not `wc -w`). The Go
tree was confirmed to build, vet, and pass its core test packages before this
was written, so no finding here is about a broken tree.

The market claims in §1, §4, and §5 are the one part of this review that
cannot be re-derived from the checkout. They were gathered from public
vendor and comparison sources in September 2026 and are perishable — the
central lesson of §4.1 is precisely that a market claim written in May was
false by September. Re-check them before acting on §6, and treat any figure
here as a starting point rather than a citation.

## Related documentation

- [Roadmap](../ROADMAP.md) — the plan this review assesses.
- [v0.3.x sequencing](v0.3.x-sequencing.md) — the amendment chain §2 traces.
- [Release cycle](methodology/release-cycle.md) — the process discussed in §3.
- [RFC 0045](rfcs/0045-open-core-extraction-policy.md) /
  [RFC 0046](rfcs/0046-budget-lease-extraction.md) — the open-core intent §4.1
  re-measures.
- [Extension specification](persatrix-extension-spec.md) — the eight-domain
  framing discussed in §1.
