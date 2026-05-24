---
id: RFC-0045
title: Open-Core Library Extraction Policy
summary: Foundational policy and governance for extracting selected Persatrix subsystems into standalone MIT-licensed repositories while the integrated product stays BUSL-1.1 — defines the license boundary, the dependency-direction invariant and its CI enforcement, the source-of-truth/sync model, contribution governance, and the naming/versioning conventions every per-extraction RFC inherits.
type: process
status: draft
author: Maksim Khomutov
created: 2026-05-24
target: v0.3.x (policy + dependency-direction CI gate) + v0.4.0+ (per-extraction RFCs)
depends_on:
  - RFC-0023
  - RFC-0024
---

# RFC 0045 — Open-Core Library Extraction Policy

**Type**: process
**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-24
**Target**: v0.3.x (policy doc + dependency-direction CI gate) + v0.4.0+ (per-extraction RFCs)
**Relates to**: RFC 0023 (LLM Call Leasing — the flagship extraction candidate), RFC 0024 (Event-Driven Agent Scheduling — the idle-loop candidate), RFC 0022 (Persona Prompt Section Templating — the prompt-safety candidate), RFC 0033 (Provider-Agnostic Model Alias Layer — adjacent to the provider/mock candidate), RFC 0029 (Personal/Society Storage Split — the memory subsystem this policy deliberately keeps BUSL)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
  - [M-1. Reusable infrastructure is locked inside a non-permissive repo](#m-1-reusable-infrastructure-is-locked-inside-a-non-permissive-repo)
  - [M-2. The decision is cross-cutting and partly irreversible](#m-2-the-decision-is-cross-cutting-and-partly-irreversible)
  - [M-3. Multiple extraction RFCs will inherit the same rules](#m-3-multiple-extraction-rfcs-will-inherit-the-same-rules)
  - [M-4. The boundary needs mechanical enforcement, not good intentions](#m-4-the-boundary-needs-mechanical-enforcement-not-good-intentions)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The open-core boundary](#a-the-open-core-boundary)
  - [B. The dependency-direction invariant](#b-the-dependency-direction-invariant)
  - [C. Source-of-truth and sync model](#c-source-of-truth-and-sync-model)
  - [D. Contribution governance](#d-contribution-governance)
  - [E. Naming, versioning, and release conventions](#e-naming-versioning-and-release-conventions)
  - [F. Repo structure: core plus adapters](#f-repo-structure-core-plus-adapters)
  - [G. The candidate set and the per-extraction RFC requirement](#g-the-candidate-set-and-the-per-extraction-rfc-requirement)
  - [H. What accepting this RFC changes in-tree](#h-what-accepting-this-rfc-changes-in-tree)
- [Security Considerations](#security-considerations)
- [Phased Rollout](#phased-rollout)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Persatrix is distributed under [BUSL-1.1](../../LICENSE): production use is not granted under the default repository terms, and each version converts to Apache 2.0 four years after its first public release. That license is correct for the integrated product — the persona society, its orchestrator, and its memory — but it is a deterrent for the *infrastructure primitives* inside the repo that have standalone value to anyone building LLM agents on any stack.

This RFC establishes the **open-core policy**: a small, deliberately chosen set of leaf subsystems is extracted into standalone **MIT-licensed** repositories that act as the top-of-funnel for developer adoption, while the integrated product — and its differentiating subsystems — stays BUSL-1.1. It does **not** move any code itself. Instead it fixes the rules that every per-extraction RFC inherits:

1. **The license boundary** — what is eligible to become MIT, and what stays BUSL.
2. **The dependency-direction invariant** — MIT code must never import BUSL code — and the CI check that enforces it.
3. **The source-of-truth and sync model** between this monorepo and the extracted repos.
4. **Contribution governance** (DCO/CLA) that preserves the BUSL core's ability to consume and, if ever needed, relicense.
5. **Naming, versioning, and release conventions** for the extracted repos.

Decide these once, here, so the per-extraction RFCs ([§G](#g-the-candidate-set-and-the-per-extraction-rfc-requirement)) are about *seam-cutting*, not policy.

## Motivation

### M-1. Reusable infrastructure is locked inside a non-permissive repo

Several subsystems are generically valuable and largely self-contained, yet a developer cannot adopt them today without taking on the whole BUSL repo. The clearest example is the per-call LLM budget lease ([RFC 0023](0023-llm-call-leasing.md)): a *gate* that acquires a server-issued lease before every model call and settles actuals after, fail-closed, multi-scope, provider-agnostic. The cost-control fear it answers is universal — the [README cost warning](../../README.md#-cost-warning) opens with the author losing real money to a single faulty idle check — and the existing market is mostly after-the-fact dashboards, not pre-call gates. That primitive has a far larger audience than Persatrix itself, but BUSL keeps that audience out.

The same is true, to varying degrees, of the persona prompt-safety snippets ([RFC 0022](0022-persona-prompt-section-templating.md)), the deterministic offline/mock provider, and the declarative agent/channel/workflow JSON schemas. These are **funnel assets**: permissive primitives that build mindshare and point adopters back toward the integrated BUSL product.

### M-2. The decision is cross-cutting and partly irreversible

A license grant is hard to walk back: once code ships under MIT, that grant cannot be retracted for the versions already published. Extraction also reshapes component boundaries (a Go/Python/Rust concern) and the security model (a leaked dependency edge becomes a license violation — [§B](#b-the-dependency-direction-invariant)). By the [RFC README](README.md#when-to-write-an-rfc), a change that "adds a new component or fundamentally changes component boundaries" or "changes the development/release process" requires an RFC. This is that RFC.

### M-3. Multiple extraction RFCs will inherit the same rules

The follow-on plan is one RFC for the flagship (budget lease) and one for the low-coupling batch (prompt-safety, mock provider, schemas), each spawning its own repository. Without a foundational policy, each of those RFCs would re-litigate licensing, governance, naming, and sync — and would likely disagree. Fixing the policy first makes the per-extraction RFCs small and consistent.

### M-4. The boundary needs mechanical enforcement, not good intentions

The single load-bearing rule of open-core — *MIT code must not import BUSL code* — cannot be left to reviewer vigilance. A future PR that adds one innocent-looking import from an extracted package into an orchestrator-internal package would, on the next mirror/release, distribute BUSL-licensed code under MIT terms. That is a licensing violation introduced by a one-line diff. The invariant must be a hard CI gate seeded at policy-acceptance time, before the first line of code is extracted.

## Goals

1. **Define the license boundary** — a checklist that decides whether a given artifact is MIT-eligible or stays BUSL ([§A](#a-the-open-core-boundary)).
2. **Establish the dependency-direction invariant** and a CI check that fails the build when an MIT-designated package imports a BUSL-only path ([§B](#b-the-dependency-direction-invariant)).
3. **Choose a source-of-truth and sync model** between the monorepo and the extracted repos, including how it may evolve per-library ([§C](#c-source-of-truth-and-sync-model)).
4. **Set contribution governance** (DCO vs CLA) that keeps inbound contributions consumable by the BUSL core and keeps relicensing freedom intact ([§D](#d-contribution-governance)).
5. **Fix naming, versioning, and release conventions** for extracted repos, including how a wire contract (e.g. a `.proto`) is versioned as public API ([§E](#e-naming-versioning-and-release-conventions)).
6. **Mandate a uniform repo structure** — framework-agnostic core plus thin per-framework adapters — as the adoption lever ([§F](#f-repo-structure-core-plus-adapters)).
7. **Name the initial candidate set** and require that each extraction be ratified by its own RFC inheriting this policy ([§G](#g-the-candidate-set-and-the-per-extraction-rfc-requirement)).

## Non-Goals

- **This RFC moves no code.** No file is relicensed, copied, or mirrored under this RFC. Each move happens under a per-extraction RFC.
- **It does not relicense the Persatrix core.** The integrated product stays BUSL-1.1 with its existing Apache-2.0 conversion schedule.
- **It does not open-source the moat.** The memory subsystem (episodic + relationship/trust + facts + working tiers, salience, society/shared pools — see [RFC 0029](0029-personal-society-storage-split.md)) and the integrated persona-society runtime stay BUSL. The [persona memory quality bar](../memory-quality-roadmap.md#quality-bar--the-dementia-test) is the differentiation; it is not given away to chase stars.
- **It is not a marketing or community-management plan.** Adoption funnels, launch posts, and docs sites are out of scope.
- **It does not design any specific seam cut.** Which exact files move, how `cost` becomes pluggable, where the gRPC/in-process split lands — all deferred to the per-extraction RFCs.

## Design / Implementation

### A. The open-core boundary

Two tiers, with a deterministic eligibility test.

**BUSL-1.1 (the product).** The integrated society and everything that makes Persatrix *Persatrix*: the orchestrator scheduler/server/executor, the persona runtime, the memory tiers and salience integration, channels governance, and the interop modules once built. This is the default — code is BUSL unless it passes the test below.

**MIT (the funnel).** Leaf infrastructure with standalone value. An artifact is **MIT-eligible** only if it passes *all* of:

1. **Standalone value.** It solves a problem an adopter has *without* Persatrix — useful behind any agent stack.
2. **Leaf position.** It has no upward dependency on product logic; it depends only on stdlib, third-party SDKs, and other MIT-tier packages ([§B](#b-the-dependency-direction-invariant)).
3. **Not the moat.** It is not the differentiating capability we sell. Memory's relationship/trust tier and salience are explicitly excluded.
4. **Generic surface.** Its public API is expressible without Persatrix-internal types leaking across the boundary.
5. **Clean provenance.** Every file is solely authored by the copyright holder *or* covered by a contributor agreement that grants relicensing rights ([§D](#d-contribution-governance)). A file containing un-cleared external contributions is **not** MIT-eligible until provenance is resolved.

The copyright holder may license his own code under MIT regardless of the repository's BUSL grant — BUSL is the grant to *users of the repo*, not a constraint on the owner. Criterion 5 exists because that freedom does **not** extend to code authored by others.

### B. The dependency-direction invariant

The one rule the whole policy rests on:

> **MIT code MUST NOT import, link, or embed BUSL code. BUSL code MAY depend on MIT code.**

The arrow points one way. The product is free to consume (and dogfood) the extracted libraries; the libraries must never reach back into the product. A violation is not a style nit — it means the next mirror or release ships BUSL-licensed source under an MIT grant.

**Enforcement (seeded at acceptance, before any extraction):**

- **Python** — an [`import-linter`](https://import-linter.readthedocs.io/) contract declaring each MIT-candidate package (e.g. the wallet client, the prompt loader/safety snippets, the provider abstraction + mock) as a layer forbidden from importing any orchestrator-coupled module. Today this lives in-tree; the contract encodes the boundary *before* the code physically moves.
- **Go** — an import-graph deny rule (e.g. [`depguard`](https://github.com/OpenPeeDeeB/depguard) or a `go list`-based check in CI) forbidding MIT-candidate packages (`internal/cost`, `internal/wallet`, the proto contract) from importing non-extractable `internal/*` packages.
- **Wiring into CI** — the check runs in the existing lint stage and is a **hard gate** (merge-blocking), alongside `make rfcs-check`, `make notices-check`, and the license checks already in the [Makefile](../../Makefile).

This mirrors a pattern the repo already uses: [RFC 0029](0029-personal-society-storage-split.md) shipped a "personal/society boundary lint rule" to keep its tiers from importing across a boundary. The open-core boundary gets the same treatment, one level up.

### C. Source-of-truth and sync model

Two viable models; the choice is per-library and may change over a library's life.

**Option A — Monorepo-canonical, mirror-out.** The Persatrix monorepo stays the single source of truth. Each extracted repo is a generated mirror (e.g. `git subtree split` to a read-only public repo). Development continues in one tree with one CI; external interest arrives as issues, and external patches are back-ported by a maintainer.
- *Pro:* one development surface, no submodule/version-skew pain, the dependency-direction check runs in the same CI that builds everything.
- *Con:* external contribution UX is second-class — contributors cannot simply open a PR against a living repo.

**Option B — Repo-canonical, consume-as-dependency.** The extracted repo becomes the source of truth; Persatrix consumes it as a versioned dependency (a Go module, a PyPI package). External contributions land directly.
- *Pro:* genuine library ergonomics; first-class external contribution; forces a clean public API.
- *Con:* cross-repo change coordination, version bumps, and release overhead — costly while the API is still moving.

**Recommendation.** Default to **Option A** while pre-1.0 and seams are still moving — it preserves velocity and keeps the boundary check honest in one CI. Flip an individual library to **Option B** once its public API has stabilized *and* external contribution demand is real. The flip is itself a documented step inside that library's extraction RFC, not a blanket switch. This is the [evolvable-over-back-compat](../development-workflow.md) stance: do not pay cross-repo coordination cost before the API has earned it.

### D. Contribution governance

Extracted repos accept outside contributions; the core must stay able to consume them.

- **MIT inbound is already compatible** with a BUSL project consuming it, so the heavyweight case (a full CLA assigning copyright) is not strictly required to *use* contributions.
- **A Developer Certificate of Origin (DCO)** — a `Signed-off-by` line asserting the contributor has the right to submit the code under the repo's license — is the lightweight, standard control. It defends against contributors injecting code they do not own (which would contaminate both the MIT library *and* the BUSL product that consumes it).
- **A CLA** is heavier but grants explicit relicensing rights. It is only needed if a future scenario requires relicensing an extracted library away from MIT — not anticipated.

**Recommendation.** **DCO on every extracted repo**; reserve a CLA only if a concrete relicensing need appears. Pair this with provenance criterion 5 in [§A](#a-the-open-core-boundary): before a file is extracted, confirm it is owner-authored or already covered. Legal confirmation is flagged in [Open Questions](#open-questions).

### E. Naming, versioning, and release conventions

- **Naming.** Either branded `persatrix-<area>` (e.g. `persatrix-budget`) or a neutral brand. Branding aids the funnel (every repo points home); neutral naming can lower adoption friction for developers wary of product-coupled libraries. The trade-off is unresolved — see [Open Questions](#open-questions) — but the *convention* (one short, area-scoped name per repo) is fixed here.
- **Versioning.** [SemVer](https://semver.org/), independent per repo, `0.x` while pre-1.0. A wire contract — notably the budget-lease `.proto` — is versioned as **public API in its own right**: a breaking proto change is a major bump, independent of the implementation's version.
- **Release hygiene, per repo.** MIT `LICENSE`; a `NOTICE`/attribution file; third-party license inventory equivalent to the repo's [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md); a `CHANGELOG`; its own CI (build + test + the dependency-direction check); and published-artifact signing once a library moves to Option B.

### F. Repo structure: core plus adapters

Every extracted library ships as a **framework-agnostic core** plus thin **`adapters/`** that bolt it onto popular agent frameworks. This is the adoption lever, not a nicety: an "official ADK / LiteLLM / LangChain integration" is its own discovery surface.

```
<library>/
  core/                      # framework-agnostic; the MIT primitive
  adapters/
    adk.py                   # e.g. before_model_callback / after_model_callback wiring
    litellm.py               # covers the LiteLLM-backed long tail (incl. CrewAI)
    langchain.py
  proto/ (if applicable)     # versioned wire contract
  examples/
  LICENSE  NOTICE  CHANGELOG  README
```

The core carries no framework imports. Each adapter is small and independently testable, and — critically — absorbs the **usage-normalization** differences between frameworks (every framework surfaces token counts differently; the adapter maps the host's usage object onto the core's settle/record signature). Adapter breadth is a per-extraction-RFC decision; the *pattern* is fixed here.

### G. The candidate set and the per-extraction RFC requirement

The initial candidates, in funnel-launch order. **Each requires its own RFC** (inheriting this policy) before any code moves; this RFC only authorizes the set and the rules.

| Candidate repo | Source subsystems | License | Why | Extraction RFC |
|----------------|-------------------|---------|-----|----------------|
| **budget-lease** (flagship) | `internal/cost` (embeddable engine) + `internal/wallet` (gRPC service) + `proto/wallet.proto` + `agents/wallet_client.py` + adapters | MIT | Universal, uncrowded, differentiated cost *gate* ([RFC 0023](0023-llm-call-leasing.md)). Idle-loop ([RFC 0024](0024-event-driven-scheduling.md)) folded in or kept internal — decided there. | To be written (first) |
| **prompt-safety kit** | `prompts/runtime/safety/*` + persona section composer + `prompt_loader` ([RFC 0022](0022-persona-prompt-section-templating.md)) | MIT | Reusable prompt-injection defenses + persona composition; near-zero coupling | To be written (batch) |
| **agent-testing / mock provider** | `LLMProvider` protocol + `MockProvider` (`llm_offline.py`) | MIT | "$0 deterministic LLM for tests" — an underserved niche, not "another router" | To be written (batch) |
| **schemas + blueprints** | `schemas/*.json` + `blueprints/*.yaml` + validator | MIT | "Define your agent team in YAML"; documentation/SEO welcome mat | To be written (batch) |
| memory tiers | `agents/memory/*` | **BUSL (kept)** | The moat — see [Non-Goals](#non-goals) | n/a |

**The per-extraction RFC contract.** Each extraction RFC must specify, at minimum: the exact files moved; the seam cuts required to compile standalone; an explicit **dependency-direction proof** (the [§B](#b-the-dependency-direction-invariant) check passes for the moved set); the chosen sync model ([§C](#c-source-of-truth-and-sync-model)); the adapter set ([§F](#f-repo-structure-core-plus-adapters)); and — for any safety-relevant code — confirmation that the extracted form preserves its safety invariants and tests (e.g. the budget lease stays fail-closed in the embeddable path).

### H. What accepting this RFC changes in-tree

Accepting RFC 0045 produces these in-tree deliverables (no extraction yet):

1. **This policy document** as the canonical reference (this file; optionally surfaced as `docs/open-core-policy.md` if a non-RFC entry point is wanted).
2. **The dependency-direction CI check** ([§B](#b-the-dependency-direction-invariant)) — Python `import-linter` contract + Go import deny rule — seeded with the candidate package list and wired into the lint stage as a hard gate.
3. **A `CONTRIBUTING`/DCO scaffold** note describing the sign-off requirement future extracted repos will carry ([§D](#d-contribution-governance)).
4. **RFC number reservations** for the follow-on extraction RFCs, recorded in the [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) per the [reservation process](README.md#reserved-rfc-numbers).

## Security Considerations

- **License-leak as a security control.** The dependency-direction invariant ([§B](#b-the-dependency-direction-invariant)) is the primary control: an MIT package importing BUSL code would distribute BUSL source under MIT terms. The CI check must be merge-blocking, and the per-extraction RFCs must include a dependency-direction proof — defense in depth against a one-line regression.
- **Supply-chain / published artifacts.** Once a library moves to Option B and publishes to PyPI / a Go module proxy, it becomes an independently consumed artifact. It needs its own release signing, third-party license inventory, and the same secret-scanning and log-redaction posture as the monorepo ([RFC 0009](0009-security-sandboxing.md), [RFC 0018](0018-structured-logging-framework.md)). The split must not carry internal config, fixtures, or secrets out of the tree.
- **Contribution provenance.** DCO/CLA ([§D](#d-contribution-governance)) plus eligibility criterion 5 ([§A](#a-the-open-core-boundary)) defend both the library and the consuming product against contributors submitting code they lack rights to relicense.
- **Safety-invariant preservation.** Some candidates encode safety claims — the budget lease is a fail-closed cost gate; the prompt-safety kit is an injection defense. Policy: extracted safety-relevant code keeps its invariants and the tests that prove them, verified in the relevant per-extraction RFC. Weakening a guarantee to fit an embeddable form is not an acceptable simplification.

## Phased Rollout

Ordered, condition-gated steps — no calendar commitments. Each step gates the next on an *event*, not a date.

1. **Policy ratified.** This RFC reaches Accepted; the boundary, invariant, sync default, and governance are fixed.
2. **Enforcement landed.** The dependency-direction CI check ([§B](#b-the-dependency-direction-invariant)) merges and is green against the current tree. *Gate for any extraction:* nothing moves until this gate exists and passes.
3. **Flagship extraction RFC.** Authored once steps 1–2 hold: the budget-lease repo, with its dependency-direction proof and sync model.
4. **Low-coupling batch RFC.** Authored after the flagship pattern is proven: prompt-safety, mock provider, schemas.
5. **Per-library Option-A→B flips.** Considered individually, each when its API has stabilized and external contribution demand is real ([§C](#c-source-of-truth-and-sync-model)) — gated on observed adoption, not a schedule.

## Open Questions

1. **Sync model default.** Confirm **Option A** (monorepo-canonical, subtree mirror) as the starting default for all candidates, or prefer **Option B** (repo-canonical dependency) from day one for any specific library? ([§C](#c-source-of-truth-and-sync-model))
2. **Governance instrument.** Is **DCO** sufficient for the MIT repos, or is a **CLA** wanted up front? Needs a brief legal read on whether consuming MIT-with-DCO contributions inside the BUSL product is fully clean. ([§D](#d-contribution-governance))
3. **Naming.** Branded `persatrix-*` (funnel value) vs a neutral brand (lower adoption friction)? ([§E](#e-naming-versioning-and-release-conventions))
4. **Dogfooding.** Should the BUSL core consume the extracted MIT libraries back as real dependencies (which proves the boundary and exercises the public API), or keep in-tree copies while pre-1.0? Recommendation: dogfood once a library reaches Option B.
5. **Event-loop placement.** Does the idle loop ([RFC 0024](0024-event-driven-scheduling.md)) ship folded into the budget-lease repo (one "$0-when-idle, gated-when-active" story), as its own repo, or stay internal? Resolve in the flagship extraction RFC, not here.
6. **`NOTICE` / third-party inventory mechanics** across the split — how [`make notices`](../../Makefile) output is partitioned per extracted repo.

## Decision / Next Steps

**Proposed decision:** adopt the open-core policy as specified — the [§A](#a-the-open-core-boundary) boundary and eligibility test, the [§B](#b-the-dependency-direction-invariant) invariant as a hard CI gate, Option A as the default sync model with per-library flips, DCO governance, and the [§F](#f-repo-structure-core-plus-adapters) core-plus-adapters structure — and keep the memory subsystem BUSL.

**On acceptance:**

1. Land the dependency-direction CI check ([§B](#b-the-dependency-direction-invariant), [§H](#h-what-accepting-this-rfc-changes-in-tree)) and confirm it is green.
2. Reserve RFC numbers for the flagship extraction RFC and the low-coupling batch RFC; record them in the [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index).
3. Author the **budget-lease extraction RFC** first; the batch RFC follows once its pattern is proven.

Sequence (ordered, no timelines): **this policy RFC → budget-lease extraction RFC → low-coupling batch RFC.**

## Related Documentation

- [LICENSE](../../LICENSE) — BUSL-1.1 terms and Apache-2.0 conversion schedule
- [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md), [NOTICE](../../NOTICE) — attribution conventions the extracted repos mirror
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — the flagship extraction candidate
- [RFC 0024 — Event-Driven Agent Scheduling](0024-event-driven-scheduling.md) — the idle-loop candidate
- [RFC 0022 — Persona Prompt Section Templating](0022-persona-prompt-section-templating.md) — the prompt-safety candidate
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — the boundary-lint precedent and the memory subsystem kept BUSL
- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — adjacent to the provider/mock candidate
- [RFC README](README.md) — RFC process, reserved numbers, format
- [BRANCHING.md](../BRANCHING.md), [development-workflow.md](../development-workflow.md) — how this RFC and its successors move through the lifecycle
