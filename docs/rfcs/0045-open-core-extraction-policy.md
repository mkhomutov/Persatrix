---
id: RFC-0045
title: Open-Core Library Extraction Policy
summary: Foundational three-tier open-core policy and governance — MIT funnel libraries below, the self-hostable BUSL-1.1 product in the middle, a never-published Private moat above. Fixes the license boundary, the MIT ← BUSL ← Private dependency-direction invariant and its CI enforcement, the source-of-truth/sync model, contribution governance, the reserved proprietary seams and a no-retraction rule, and the naming/versioning conventions every per-extraction RFC inherits. Moves no code and stands up no private track.
type: process
status: proposed
author: Maksim Khomutov
created: 2026-05-24
target: v0.3.x (policy + dependency-direction CI gate) + v0.4.0+ (per-extraction RFCs)
depends_on:
  - RFC-0023
  - RFC-0024
  - RFC-0029
---

# RFC 0045 — Open-Core Library Extraction Policy

**Type**: process
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-24
**Target**: v0.3.x (policy doc + dependency-direction CI gate) + v0.4.0+ (per-extraction RFCs)
**Relates to**: RFC 0023 (LLM Call Leasing — the flagship extraction candidate), RFC 0024 (Event-Driven Agent Scheduling — the idle-loop candidate), RFC 0022 (Persona Prompt Section Templating — the prompt-safety candidate), RFC 0029 (Personal/Society Storage Split — the memory tiers kept BUSL and the managed society backend reserved for the Private tier), RFC 0033 (Provider-Agnostic Model Alias Layer — adjacent to the provider/mock candidate), RFC 0012 (Protocols & Organizations) and RFC 0039 (User Accounts & Authentication — the identity/tenancy seams the Private tier attaches to)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
  - [M-1. Reusable infrastructure is locked inside a non-permissive repo](#m-1-reusable-infrastructure-is-locked-inside-a-non-permissive-repo)
  - [M-2. The decision is cross-cutting and partly irreversible](#m-2-the-decision-is-cross-cutting-and-partly-irreversible)
  - [M-3. Multiple extraction RFCs will inherit the same rules](#m-3-multiple-extraction-rfcs-will-inherit-the-same-rules)
  - [M-4. The boundary needs mechanical enforcement, not good intentions](#m-4-the-boundary-needs-mechanical-enforcement-not-good-intentions)
  - [M-5. Exclusivity runs in both directions](#m-5-exclusivity-runs-in-both-directions)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The three-tier boundary](#a-the-three-tier-boundary)
  - [B. The dependency-direction invariant](#b-the-dependency-direction-invariant)
  - [C. The private tier: reserved seams and the no-retraction rule](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)
  - [D. Source-of-truth and sync model](#d-source-of-truth-and-sync-model)
  - [E. Contribution governance](#e-contribution-governance)
  - [F. Naming, versioning, and release conventions](#f-naming-versioning-and-release-conventions)
  - [G. Repo structure: core plus adapters](#g-repo-structure-core-plus-adapters)
  - [H. The candidate set and the per-extraction RFC requirement](#h-the-candidate-set-and-the-per-extraction-rfc-requirement)
  - [I. What accepting this RFC changes in-tree](#i-what-accepting-this-rfc-changes-in-tree)
- [Security Considerations](#security-considerations)
- [Phased Rollout](#phased-rollout)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Persatrix is distributed under [BUSL-1.1](../../LICENSE): production use is not granted under the default repository terms, and each version converts to Apache 2.0 four years after its first public release. That license is correct for the integrated, self-hostable product — the persona society, its orchestrator, and its memory — but it is the wrong license at *both* edges of the stack. Below it sit infrastructure primitives that have standalone value to anyone building LLM agents on any framework, and those should be **more** open. Above it sits the durable commercial differentiation, and that should be **less** open.

This RFC establishes a **three-tier open-core policy**:

- **MIT (below) — the funnel.** A small, deliberately chosen set of leaf primitives extracted into standalone MIT repositories that drive developer adoption.
- **BUSL-1.1 (middle) — the product.** The full, honest, self-hostable single-node society. The trust tier.
- **Private (above) — the moat.** Hosted/commercial capabilities that are never published: the managed multi-tenant control plane, real billing, identity/tenancy, the managed and scaled society backend, and future advanced capabilities. The revenue tier.

It **moves no code** and **stands up no private repository or parallel development track.** It fixes the rules every per-extraction RFC inherits — the license boundary, the `MIT ← BUSL ← Private` dependency-direction invariant and its CI gate, the sync model, governance, the *reserved proprietary seams*, and a **no-retraction rule** that keeps the open product honest — so the per-extraction RFCs ([§H](#h-the-candidate-set-and-the-per-extraction-rfc-requirement)) are about *seam-cutting*, not policy. The standing-up of an actual Private tier earns its own commercial-architecture RFC **later**, gated on a forcing function ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)).

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

### M-5. Exclusivity runs in both directions

The funnel argument (M-1) pulls code *down* into MIT. The mirror-image question is what should be pulled *up*, out of BUSL, into a closed tier — and the honest answer is that **BUSL is a delay, not a moat.** Every version converts to Apache 2.0 four years after release. For fast-moving agent code that delay barely bites — four-release-old source is usually superseded long before it converts — which means *durable* commercial differentiation cannot rest on BUSL source secrecy. It rests on the things you operate and never ship: a managed multi-tenant control plane, real metering and billing (versus the in-app wallet *simulation*), identity/tenancy, and a managed, scaled society/memory backend (the Postgres society tier reserved in [RFC 0029](0029-personal-society-storage-split.md)).

Two consequences follow, and both are policy, not implementation:

1. **The moat is operational and forward-looking, not retracted.** The durable edge is the *managed and scaled* backend plus *future* advanced capabilities that ship straight to the Private tier — not source that is already public in BUSL today.
2. **A no-retraction rule is required.** Clawing a shipped capability out of the open product into a closed tier is the reputational "rug pull" that open-core history punishes far more severely than any four-year clock costs you. The line between BUSL and Private must therefore be drawn *before* code is published, not after. This RFC reserves the seams; it does not move the wall inward on anything already public.

## Goals

1. **Define the three-tier boundary** — MIT / BUSL / Private — and an eligibility test that decides which tier a given artifact belongs to ([§A](#a-the-three-tier-boundary)).
2. **Establish the dependency-direction invariant** `MIT ← BUSL ← Private`, and a CI check that fails the build when a lower tier imports a higher one ([§B](#b-the-dependency-direction-invariant)).
3. **Reserve the proprietary seams and fix a no-retraction rule** — enumerate the interfaces a future Private tier attaches to, and forbid retracting any currently-public capability — *without* standing up a private repo or parallel track now ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)).
4. **Choose a source-of-truth and sync model** between the monorepo and the extracted MIT repos, including how it may evolve per-library ([§D](#d-source-of-truth-and-sync-model)).
5. **Set contribution governance** (DCO vs CLA) that keeps inbound contributions consumable by the BUSL core and keeps relicensing freedom intact ([§E](#e-contribution-governance)).
6. **Fix naming, versioning, and release conventions** for extracted repos, including how a wire contract (e.g. a `.proto`) is versioned as public API ([§F](#f-naming-versioning-and-release-conventions)).
7. **Mandate a uniform repo structure** — framework-agnostic core plus thin per-framework adapters — as the adoption lever ([§G](#g-repo-structure-core-plus-adapters)).
8. **Name the initial candidate set** and require that each extraction be ratified by its own RFC inheriting this policy ([§H](#h-the-candidate-set-and-the-per-extraction-rfc-requirement)).

## Non-Goals

- **This RFC moves no code.** No file is relicensed, copied, or mirrored under this RFC. Each move happens under a per-extraction RFC.
- **It stands up no Private tier.** No private repository is created and no parallel private development track is staffed. This RFC *reserves the seams*; the actual Private build is deferred to its own commercial-architecture RFC, gated on a forcing function ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)).
- **It retracts nothing.** No capability currently shipped under BUSL is moved into the Private tier. The memory tiers as they exist today — episodic, relationship/trust, facts, working ([RFC 0029](0029-personal-society-storage-split.md)) — stay BUSL and self-hostable. The Private differentiation is the *managed/scaled* society backend and *future* advanced modeling, not the current source.
- **It does not relicense the Persatrix core.** The integrated product stays BUSL-1.1 with its existing Apache-2.0 conversion schedule.
- **It is not a marketing or community-management plan.** Adoption funnels, launch posts, and docs sites are out of scope.
- **It does not design any specific seam cut.** Which exact files move, how `cost` becomes pluggable, where the gRPC/in-process split lands — all deferred to the per-extraction RFCs.

## Design / Implementation

### A. The three-tier boundary

Three tiers, with a deterministic placement test. The default tier is BUSL — code is BUSL unless it is pulled down to MIT (passing the test below) or is *born* in the Private tier (never published).

**MIT (the funnel).** Leaf infrastructure with standalone value. An artifact is **MIT-eligible** only if it passes *all* of:

1. **Standalone value.** It solves a problem an adopter has *without* Persatrix — useful behind any agent stack.
2. **Leaf position.** It has no upward dependency on product or Private logic; it depends only on stdlib, third-party SDKs, and other MIT-tier packages ([§B](#b-the-dependency-direction-invariant)).
3. **Not the moat.** It is not the differentiating capability we sell.
4. **Generic surface.** Its public API is expressible without Persatrix-internal types leaking across the boundary.
5. **Clean provenance.** Every file is solely authored by the copyright holder *or* covered by a contributor agreement that grants relicensing rights ([§E](#e-contribution-governance)). A file containing un-cleared external contributions is **not** MIT-eligible until provenance is resolved.

**BUSL-1.1 (the product) — the trust tier.** The full, honest, self-hostable single-node society: the orchestrator scheduler/server/executor, the persona runtime, the memory tiers (episodic + relationship/trust + facts + working) and salience integration, channels governance, and the interop modules once built. It must remain genuinely capable — not crippleware. This is the default tier.

**Private (the moat) — the revenue tier.** Capabilities that are never published and are operated, not shipped: the managed multi-tenant control plane / hosted orchestrator; real metering and billing (the in-app wallet is a *simulation*); identity and tenancy (SSO, RBAC, org administration — the surface [RFC 0039](0039-user-accounts-authentication.md) and [RFC 0012](0012-protocols-organizations.md) gesture at); the managed, scaled society/memory backend (the Postgres society tier of [RFC 0029](0029-personal-society-storage-split.md), operated as a service); enterprise connectors/bridges at scale; and mesh-as-a-cluster. Per [§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule), the Private tier is forward-looking — it is fed by new capability and by operating the backend, not by retracting public source.

The copyright holder may license his own code under any terms regardless of the repository's BUSL grant — BUSL is the grant to *users of the repo*, not a constraint on the owner. This is what makes both the downward (MIT) extraction and an upward (Private) tier built on the BUSL core legitimate. The freedom does **not** extend to code authored by others (criterion 5).

### B. The dependency-direction invariant

The rule the whole policy rests on, now three-layered:

> **Imports may only point down-tier: `MIT ← BUSL ← Private`. A higher tier may depend on a lower tier; a lower tier must never import a higher one.**

Concretely: MIT must not import BUSL or Private; BUSL must not import Private; BUSL *may* consume (and dogfood) MIT; Private *may* consume BUSL and MIT. A lower-tier package reaching up is not a style nit — for the MIT↛BUSL edge it means the next mirror or release ships BUSL-licensed source under an MIT grant.

**Enforcement (seeded at acceptance, before any extraction):**

- **Python** — an [`import-linter`](https://import-linter.readthedocs.io/) contract declaring each MIT-candidate package (the wallet client, the prompt loader/safety snippets, the provider abstraction + mock) as a layer forbidden from importing any orchestrator-coupled module. The contract encodes the boundary *before* the code physically moves.
- **Go** — an import-graph deny rule (e.g. [`depguard`](https://github.com/OpenPeeDeeB/depguard) or a `go list`-based check in CI) forbidding MIT-candidate packages (`internal/cost`, `internal/wallet`, the proto contract) from importing non-extractable `internal/*` packages.
- **Wiring into CI** — the check runs in the existing lint stage and is a **hard gate** (merge-blocking), alongside `make rfcs-check`, `make notices-check`, and the license checks already in the [Makefile](../../Makefile).
- **The BUSL↛Private edge is, for now, a documentation and interface-design discipline, not a live check.** No Private code exists in this repo, so there is nothing to import. The edge becomes enforceable once the Private tier exists as a separate closed repository that depends inward; until then it is enforced by *keeping intended-private capability out of the public tree in the first place* ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)).

This mirrors a pattern the repo already uses: [RFC 0029](0029-personal-society-storage-split.md) shipped a "personal/society boundary lint rule." The open-core boundary gets the same treatment, one level up.

### C. The private tier: reserved seams and the no-retraction rule

The discipline here is **reserve the seam, defer the track.** Pre-1.0, with a small team and no customers, standing up a parallel private codebase is pure carrying cost plus an integration tax — every BUSL change risks breaking the layer above, forcing stable internal interfaces and split attention before there is revenue to justify either. So this RFC defines *where* a Private tier attaches and forbids the moves that would poison the open product, then stops.

**Reserved proprietary seams.** These are the interfaces a future Private tier plugs into. The architecture is already trending toward most of them, which is why reserving them now is nearly free and retrofitting later is painful:

| Seam | Interface to keep stable | Already gestured at by |
|------|--------------------------|------------------------|
| **Memory backend** | A `MemoryBackend` boundary behind the frozen memory facade, so a managed/scaled society store is a drop-in | [RFC 0029](0029-personal-society-storage-split.md) `society_facade` raising `NotAvailable`; the frozen memory facade ahead of the Postgres split |
| **Budget policy & pricing/metering** | A pluggable budget-policy and pricing/metering source, so real billing replaces the in-app simulation | the wallet's pluggable cost/budget interface ([RFC 0023](0023-llm-call-leasing.md)) |
| **Identity & authz** | An `Identity`/authz boundary for SSO, RBAC, org administration | [RFC 0039](0039-user-accounts-authentication.md); [RFC 0012](0012-protocols-organizations.md); `config/organizations.yaml` |
| **Control plane & tenancy** | A control-plane/tenancy boundary for a managed, multi-tenant orchestrator | the single-node orchestrator topology |

**The no-retraction rule.** Nothing currently published under BUSL is moved into the Private tier. The line between BUSL and Private is drawn *before* code is published. In practice:

- The memory tiers as they exist today — including **relationship/trust** — stay BUSL and self-hostable. They are not clawed back onto a closed tier.
- The Private differentiation is the **managed, scaled society backend** (operated, not shipped) plus **future, more-advanced** capabilities that ship straight to Private and were never public.
- Corollary: do not ship a destined-for-private capability under BUSL "for now" and reclaim it later. If a capability is intended to be private, it stays out of the public tree from the start.
- **Ambiguous future capabilities default to BUSL.** When it is genuinely unclear whether a *new* capability belongs in BUSL or Private, it ships to BUSL — the default tier — and the line is drawn explicitly in that capability's own RFC. Defaulting open is safe precisely because the moat is operational, not source-secrecy ([M-5](#m-5-exclusivity-runs-in-both-directions)): shipping the source to BUSL does not weaken a moat that lives in the *managed, scaled* operation. The asymmetry only cuts one way under the no-retraction rule — a capability shipped to BUSL can always stay or be opened further, but one wrongly withheld can still be released later, whereas a published one can never be pulled back.

**Reserve, then stop.** Define the seams above as stable interfaces, keep the three-tier import discipline ([§B](#b-the-dependency-direction-invariant)) in mind when shaping them, and do not populate or parallel-develop a private repo. **Flip from reserved seam to active private track only on a forcing function:** a paying design partner, a hosted offering actually committed to ship, or a feature that is inherently managed (multi-tenant control plane, shared abuse/safety infrastructure). Until then the Private layer is a *thin overlay on stable interfaces*, not a forked codebase.

### D. Source-of-truth and sync model

Two viable models for the MIT repos; the choice is per-library and may change over a library's life.

**Option A — Monorepo-canonical, mirror-out.** The Persatrix monorepo stays the single source of truth. Each extracted repo is a generated mirror (e.g. `git subtree split` to a read-only public repo). Development continues in one tree with one CI; external interest arrives as issues, and external patches are back-ported by a maintainer.
- *Pro:* one development surface, no submodule/version-skew pain, the dependency-direction check runs in the same CI that builds everything.
- *Con:* external contribution UX is second-class — contributors cannot simply open a PR against a living repo.

**Option B — Repo-canonical, consume-as-dependency.** The extracted repo becomes the source of truth; Persatrix consumes it as a versioned dependency (a Go module, a PyPI package). External contributions land directly.
- *Pro:* genuine library ergonomics; first-class external contribution; forces a clean public API.
- *Con:* cross-repo change coordination, version bumps, and release overhead — costly while the API is still moving.

**Decision.** Default to **Option A** while pre-1.0 and seams are still moving. Flip an individual library to **Option B** once its public API has stabilized *and* external contribution demand is real. The flip is itself a documented step inside that library's extraction RFC, not a blanket switch. This is the [evolvable-over-back-compat](../development-workflow.md) stance: do not pay cross-repo coordination cost before the API has earned it.

**Dogfooding follows the sync model.** While a library is on Option A it remains an in-tree copy and the BUSL core does not take a dependency on its mirror — there is no second source of truth to depend on. When a library flips to Option B, the core consumes it back as a versioned dependency. That flip is what makes dogfooding meaningful: depending on the published artifact is what actually proves the dependency-direction boundary ([§B](#b-the-dependency-direction-invariant)) and exercises the public API against a real consumer. So dogfooding is not a separate decision — it is the Option-B side of the per-library flip above.

### E. Contribution governance

Extracted repos accept outside contributions; the core must stay able to consume them.

- **MIT inbound is already compatible** with a BUSL project consuming it, so the heavyweight case (a full CLA assigning copyright) is not strictly required to *use* contributions.
- **A Developer Certificate of Origin (DCO)** — a `Signed-off-by` line asserting the contributor has the right to submit the code under the repo's license — is the lightweight, standard control. It defends against contributors injecting code they do not own (which would contaminate both the MIT library *and* the BUSL product that consumes it).
- **A CLA** is heavier but grants explicit relicensing rights. It is only needed if a future scenario requires relicensing an extracted library away from MIT — not anticipated.

**Decision.** **DCO on every extracted repo**; reserve a CLA only if a concrete relicensing need appears. Pair this with provenance criterion 5 in [§A](#a-the-three-tier-boundary): before a file is extracted, confirm it is owner-authored or already covered. One belt-and-suspenders confirmation remains before the first repo accepts outside contributions — a brief legal read that MIT-with-DCO inbound is cleanly consumable inside the BUSL product — carried as the single residual [Open Question](#open-questions). It is expected to confirm, not overturn, the DCO choice (MIT is permissive and downstream-compatible), so it does not gate ratifying this policy.

### F. Naming, versioning, and release conventions

- **Naming.** Branded `persatrix-<area>` (e.g. `persatrix-budget`) — one short, area-scoped name per repo. The funnel is the stated purpose of the MIT tier ([M-1](#m-1-reusable-infrastructure-is-locked-inside-a-non-permissive-repo), [§G](#g-repo-structure-core-plus-adapters)), and a branded name makes every repo a pointer back to the product, which is the discoverability the funnel relies on. The friction cost of product-coupled naming is real but secondary; a specific library may argue for a neutral name in its own extraction RFC if it can show that adoption friction dominates the funnel benefit for that primitive.
- **Versioning.** [SemVer](https://semver.org/), independent per repo, `0.x` while pre-1.0. A wire contract — notably the budget-lease `.proto` — is versioned as **public API in its own right**: a breaking proto change is a major bump, independent of the implementation's version.
- **Release hygiene, per repo.** MIT `LICENSE`; a `NOTICE`/attribution file; third-party license inventory equivalent to the repo's [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md); a `CHANGELOG`; its own CI (build + test + the dependency-direction check); and published-artifact signing once a library moves to Option B.
- **Third-party inventory is per-repo, not partitioned.** The monorepo's [`make notices`](../../Makefile) output is **not** sliced up across extracted repos. Each repo regenerates its own inventory from *its own* dependency closure — an extracted MIT primitive depends on a small, different set than the monorepo, so a fresh per-repo `notices` run is both simpler and more accurate than carving the monorepo's. The mechanic (which generator each repo runs) is a per-extraction-RFC detail; the principle — independent, self-scoped inventory per repo — is fixed here.

### G. Repo structure: core plus adapters

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

### H. The candidate set and the per-extraction RFC requirement

The initial MIT candidates, in funnel-launch order. **Each requires its own RFC** (inheriting this policy) before any code moves; this RFC only authorizes the set and the rules.

| Candidate repo | Source subsystems | Tier | Why | Extraction RFC |
|----------------|-------------------|------|-----|----------------|
| **budget-lease** (flagship) | `internal/cost` (embeddable engine) + `internal/wallet` (gRPC service) + `proto/wallet.proto` + `agents/wallet_client.py` + adapters | MIT | Universal, uncrowded, differentiated cost *gate* ([RFC 0023](0023-llm-call-leasing.md)). Idle-loop ([RFC 0024](0024-event-driven-scheduling.md)) folded in or kept internal — decided there. | To be written (first) |
| **prompt-safety kit** | `prompts/runtime/safety/*` + persona section composer + `prompt_loader` ([RFC 0022](0022-persona-prompt-section-templating.md)) | MIT | Reusable prompt-injection defenses + persona composition; near-zero coupling | To be written (batch) |
| **agent-testing / mock provider** | `LLMProvider` protocol + `MockProvider` (`llm_offline.py`) | MIT | "$0 deterministic LLM for tests" — an underserved niche, not "another router" | To be written (batch) |
| **schemas + blueprints** | `schemas/*.json` + `blueprints/*.yaml` + validator | MIT | "Define your agent team in YAML"; documentation/SEO welcome mat | To be written (batch) |
| memory tiers (current) | `agents/memory/*` incl. relationship/trust | **BUSL (kept, not retracted)** | The self-hostable trust tier — see [§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule) and [Non-Goals](#non-goals) | n/a |
| managed society backend + next-gen modeling | the operated Postgres society tier ([RFC 0029](0029-personal-society-storage-split.md)) + future advanced capabilities | **Private (reserved seam, deferred)** | The durable, operational moat — built only on a forcing function | Commercial-architecture RFC, deferred |

**The per-extraction RFC contract.** Each extraction RFC must specify, at minimum: the exact files moved; the seam cuts required to compile standalone; an explicit **dependency-direction proof** (the [§B](#b-the-dependency-direction-invariant) check passes for the moved set); the chosen sync model ([§D](#d-source-of-truth-and-sync-model)); the adapter set ([§G](#g-repo-structure-core-plus-adapters)); and — for any safety-relevant code — confirmation that the extracted form preserves its safety invariants and tests (e.g. the budget lease stays fail-closed in the embeddable path).

### I. What accepting this RFC changes in-tree

Accepting RFC 0045 produces these in-tree deliverables (no extraction, no private tier):

1. **This policy document** as the canonical reference (this file; optionally surfaced as `docs/open-core-policy.md` if a non-RFC entry point is wanted).
2. **The dependency-direction CI check** ([§B](#b-the-dependency-direction-invariant)) — Python `import-linter` contract + Go import deny rule — seeded with the candidate package list and wired into the lint stage as a hard gate. (The MIT↛BUSL edge is live; the BUSL↛Private edge is documentation-only until a Private repo exists.)
3. **A reserved-seams note** ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)) recording the four plug-points and the no-retraction rule, so future BUSL work shapes those interfaces deliberately.
4. **A `CONTRIBUTING`/DCO scaffold** note describing the sign-off requirement future extracted repos will carry ([§E](#e-contribution-governance)).
5. **RFC number reservations** for the follow-on extraction RFCs, recorded in the [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) per the [reservation process](README.md#reserved-rfc-numbers). The commercial-architecture (Private) RFC is *named but not numbered* until its forcing function arrives.

## Security Considerations

- **License-leak as a security control.** The dependency-direction invariant ([§B](#b-the-dependency-direction-invariant)) is the primary control: an MIT package importing BUSL code would distribute BUSL source under MIT terms. The CI check must be merge-blocking, and the per-extraction RFCs must include a dependency-direction proof — defense in depth against a one-line regression.
- **No leakage of destined-for-private capability.** The no-retraction rule ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)) is also a confidentiality control: intended-private capability must not be published under BUSL "for now" and reclaimed later — both because retraction is a reputational hazard and because a published version cannot be un-published. Decide the line before shipping.
- **Supply-chain / published artifacts.** Once a library moves to Option B and publishes to PyPI / a Go module proxy, it becomes an independently consumed artifact. It needs its own release signing, third-party license inventory, and the same secret-scanning and log-redaction posture as the monorepo ([RFC 0009](0009-security-sandboxing.md), [RFC 0018](0018-structured-logging-framework.md)). The split must not carry internal config, fixtures, or secrets out of the tree.
- **Contribution provenance.** DCO/CLA ([§E](#e-contribution-governance)) plus eligibility criterion 5 ([§A](#a-the-three-tier-boundary)) defend both the library and the consuming product against contributors submitting code they lack rights to relicense.
- **Safety-invariant preservation.** Some candidates encode safety claims — the budget lease is a fail-closed cost gate; the prompt-safety kit is an injection defense. Policy: extracted safety-relevant code keeps its invariants and the tests that prove them, verified in the relevant per-extraction RFC. Weakening a guarantee to fit an embeddable form is not an acceptable simplification.

## Phased Rollout

Ordered, condition-gated steps — no calendar commitments. Each step gates the next on an *event*, not a date.

1. **Policy ratified.** This RFC reaches Accepted; the three-tier boundary, invariant, reserved seams, no-retraction rule, sync default, and governance are fixed.
2. **Enforcement landed.** The dependency-direction CI check ([§B](#b-the-dependency-direction-invariant)) merges and is green against the current tree, and the reserved-seams note is recorded. *Gate for any extraction:* nothing moves until this gate exists and passes.
3. **Flagship extraction RFC.** Authored once steps 1–2 hold: the budget-lease repo, with its dependency-direction proof and sync model.
4. **Low-coupling batch RFC.** Authored after the flagship pattern is proven: prompt-safety, mock provider, schemas.
5. **Per-library Option-A→B flips.** Considered individually, each when its API has stabilized and external contribution demand is real ([§D](#d-source-of-truth-and-sync-model)) — gated on observed adoption, not a schedule.
6. **Commercial-architecture (Private) RFC.** Authored only when a forcing function arrives ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)) — a paying design partner, a committed hosted offering, or an inherently-managed feature. Not before.

## Open Questions

The policy questions raised in earlier drafts are now resolved in-section:

- **Sync model default → Option A** (monorepo-canonical, mirror-out), with a per-library flip to Option B documented inside each extraction RFC ([§D](#d-source-of-truth-and-sync-model)).
- **Dogfooding → in-tree under Option A, real versioned dependency under Option B** — the Option-B side of the same per-library flip, not a separate decision ([§D](#d-source-of-truth-and-sync-model)).
- **Governance → DCO** on every extracted repo, paired with provenance criterion 5; a CLA is reserved only for a concrete future relicensing need ([§E](#e-contribution-governance)).
- **Naming → branded `persatrix-<area>`**, one short area-scoped name per repo, with a per-extraction override path for a primitive that can show neutral naming wins ([§F](#f-naming-versioning-and-release-conventions)).
- **Third-party inventory → per-repo, independently regenerated** from each repo's own dependency closure; the monorepo's `make notices` is not partitioned ([§F](#f-naming-versioning-and-release-conventions)).
- **BUSL/Private line for an ambiguous future capability → default to BUSL**, drawn explicitly in that capability's own RFC ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)).

**Genuinely open:**

1. **Legal confirmation of the DCO path.** A brief legal read that MIT-with-DCO inbound contributions are cleanly consumable inside the BUSL product. It is expected to confirm — not overturn — the [§E](#e-contribution-governance) DCO decision, since MIT is permissive and downstream-compatible, but it should be obtained before the first extracted repo accepts outside contributions. It does **not** gate ratifying this policy.

**Deferred to successor RFCs (not open for this policy):**

- **Event-loop placement.** Whether the idle loop ([RFC 0024](0024-event-driven-scheduling.md)) folds into the budget-lease repo (one "$0-when-idle, gated-when-active" story), ships as its own repo, or stays internal is a seam-cut decided in the flagship extraction RFC ([§H](#h-the-candidate-set-and-the-per-extraction-rfc-requirement)) — a question for that RFC, not a policy choice here.

## Decision / Next Steps

**Proposed decision:** adopt the three-tier open-core policy as specified — the [§A](#a-the-three-tier-boundary) boundary and eligibility test, the [§B](#b-the-dependency-direction-invariant) `MIT ← BUSL ← Private` invariant as a hard CI gate, the [§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule) reserved seams and no-retraction rule (reserve, don't staff), Option A as the default sync model with per-library flips (dogfooding on the Option-B side), DCO governance, branded `persatrix-<area>` naming, and the [§G](#g-repo-structure-core-plus-adapters) core-plus-adapters structure — and keep the current memory tiers (including relationship/trust) BUSL, with the managed society backend reserved for a deferred Private tier. One non-blocking item remains open — a legal read confirming the DCO path ([Open Questions](#open-questions)).

**On acceptance:**

1. Land the dependency-direction CI check ([§B](#b-the-dependency-direction-invariant), [§I](#i-what-accepting-this-rfc-changes-in-tree)) and the reserved-seams note; confirm the check is green.
2. Reserve RFC numbers for the flagship extraction RFC and the low-coupling batch RFC; record them in the [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index). The commercial-architecture (Private) RFC is named but deferred.
3. Author the **budget-lease extraction RFC** first; the batch RFC follows once its pattern is proven.

Sequence (ordered, no timelines): **this policy RFC → budget-lease extraction RFC → low-coupling batch RFC.** A fourth, commercial-architecture RFC is deferred until a forcing function exists ([§C](#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)).

## Related Documentation

- [LICENSE](../../LICENSE) — BUSL-1.1 terms and Apache-2.0 conversion schedule
- [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md), [NOTICE](../../NOTICE) — attribution conventions the extracted repos mirror
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — the flagship extraction candidate and the budget/metering seam
- [RFC 0024 — Event-Driven Agent Scheduling](0024-event-driven-scheduling.md) — the idle-loop candidate
- [RFC 0022 — Persona Prompt Section Templating](0022-persona-prompt-section-templating.md) — the prompt-safety candidate
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — the boundary-lint precedent, the memory tiers kept BUSL, and the managed society backend reserved for the Private tier
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md), [RFC 0012 — Protocols & Organizations](0012-protocols-organizations.md) — the identity/tenancy seams the Private tier attaches to
- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — adjacent to the provider/mock candidate
- [RFC README](README.md) — RFC process, reserved numbers, format
- [BRANCHING.md](../BRANCHING.md), [development-workflow.md](../development-workflow.md) — how this RFC and its successors move through the lifecycle
