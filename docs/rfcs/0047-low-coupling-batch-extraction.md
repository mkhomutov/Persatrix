---
id: RFC-0047
title: Low-Coupling Batch Library Extraction (prompt kit, mock LLM, schemas)
summary: Second per-extraction RFC under RFC 0045, covering the three low-coupling MIT candidates as one batch — the prompt kit (injection-defense snippets + persona-composition toolkit), the deterministic mock LLM (a test double, not a router), and the agent/channel/workflow JSON schemas. Each ships as its own single-identity repo following the RFC 0046 precedent: persatrix-prompts (Python loader + behavior renderer + the prompts/runtime data), persatrix-mock-llm (the LLMProvider protocol + MockProvider), and persatrix-schemas (language-neutral JSON Schema data + a thin reference validator). The one real seam-cut is the prompt composer: prompt_loader + persona_behavior + data move MIT, while the persona-state-coupled prompt_assembly mixin stays BUSL and consumes them. The real LLM providers + factory stay BUSL; the schemas stay consumed in-tree by the Go planner, Python validator, and Rust CLI under Option A. Batched because all three are leaf with near-zero seam-cutting and inherit the same policy. Code moves only after RFC 0045 is accepted and its boundary CI gate is green.
type: architecture
status: proposed
author: Maksim Khomutov
created: 2026-05-25
target: v0.4.0+ (gated on RFC-0045 acceptance + the MIT↛BUSL boundary CI gate)
depends_on:
  - RFC-0045
  - RFC-0046
  - RFC-0022
  - RFC-0009
---

# RFC 0047 — Low-Coupling Batch Library Extraction (prompt kit, mock LLM, schemas)

**Type**: architecture  
**Status**: 📋 Proposed  
**Author**: Maksim Khomutov  
**Date**: 2026-05-25  
**Target**: v0.4.0+ (gated on RFC-0045 acceptance + the MIT↛BUSL boundary CI gate)  
**Depends on**: RFC 0045 (Open-Core Library Extraction Policy — the governing policy this RFC inherits), RFC 0046 (Budget-Lease Extraction — the flagship whose single-identity/audience→artifact precedent this RFC follows), RFC 0022 (Persona Prompt Section Templating — the prompt kit's design), RFC 0009 (Security & Sandboxing — the external-data envelope and injection-defense snippets whose invariants must survive extraction)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
  - [M-1. Three leaf primitives are trapped behind BUSL with the flagship](#m-1-three-leaf-primitives-are-trapped-behind-busl-with-the-flagship)
  - [M-2. One RFC, three repos — batching is correct here](#m-2-one-rfc-three-repos--batching-is-correct-here)
  - [M-3. The RFC 0046 single-identity precedent applies to each](#m-3-the-rfc-0046-single-identity-precedent-applies-to-each)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The batch at a glance](#a-the-batch-at-a-glance)
  - [B. `persatrix-prompts` — the prompt kit (the one real seam-cut)](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut)
  - [C. `persatrix-mock-llm` — the deterministic test double](#c-persatrix-mock-llm--the-deterministic-test-double)
  - [D. `persatrix-schemas` — language-neutral config schemas](#d-persatrix-schemas--language-neutral-config-schemas)
  - [E. Dependency-direction proof](#e-dependency-direction-proof)
  - [F. Sync model and dogfooding](#f-sync-model-and-dogfooding)
  - [G. Adapters, per repo](#g-adapters-per-repo)
  - [H. Relationship to RFC 0045 (the refinement)](#h-relationship-to-rfc-0045-the-refinement)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

This is the **second per-extraction RFC** under the [RFC 0045](0045-open-core-extraction-policy.md) open-core policy. Where [RFC 0046](0046-budget-lease-extraction.md) carves out the flagship (the budget lease), this RFC carves out the **low-coupling batch** — the three remaining MIT candidates [RFC 0045 §H](0045-open-core-extraction-policy.md#h-the-candidate-set-and-the-per-extraction-rfc-requirement) names:

1. **`persatrix-prompts`** — a Python kit of prompt-injection **defenses** (the `<|user_message|>` delimiter rule, the `<external_data>` envelope, reply-discretion) plus a **persona-composition** toolkit (the section templates, the behavioral-dimension renderer, the deny-by-default loader).
2. **`persatrix-mock-llm`** — the `LLMProvider` protocol + the deterministic `MockProvider`, positioned as a **$0 test double**, not "another router."
3. **`persatrix-schemas`** — the agent/channel/workflow/optimization **JSON schemas** (language-neutral data) + the example blueprints/templates + a thin reference validator.

All three are **leaf** subsystems with near-zero seam-cutting — which is exactly why they are batched into one RFC rather than re-litigating [RFC 0045](0045-open-core-extraction-policy.md)'s policy three times ([§M-2](#m-2-one-rfc-three-repos--batching-is-correct-here)). Each still ships as its **own single-identity repo**, following the [RFC 0046](0046-budget-lease-extraction.md) precedent that an extracted artifact must have one language and one purpose, not be a polyglot grab-bag ([§M-3](#m-3-the-rfc-0046-single-identity-precedent-applies-to-each)).

The **one genuine seam-cut** is in the prompt kit: the leaf loader (`prompt_loader.py`) and behavioral-dimension renderer (`persona_behavior.py`) plus the `prompts/runtime/` data move to MIT, while the persona-state-coupled composer (`prompt_assembly.py`) **stays BUSL and consumes the kit** ([§B](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut)). The mock and the schemas need no cut — they are already leaf. Per the policy, **no code moves until [RFC 0045](0045-open-core-extraction-policy.md) is Accepted and its boundary CI gate is green** ([Decision](#decision--next-steps)).

## Motivation

### M-1. Three leaf primitives are trapped behind BUSL with the flagship

[RFC 0045 §M-1](0045-open-core-extraction-policy.md#m-1-reusable-infrastructure-is-locked-inside-a-non-permissive-repo) names four MIT funnel candidates; [RFC 0046](0046-budget-lease-extraction.md) extracts the first. The remaining three each answer a real, framework-independent need:

- **Prompt-injection defenses are reusable security primitives.** The delimiter rule, the external-data envelope, and reply-discretion ([RFC 0009](0009-security-sandboxing.md), [RFC 0022](0022-persona-prompt-section-templating.md)) are prompts-as-data any agent builder can drop into a system prompt regardless of stack. The persona-composition toolkit beside them answers a second need — "compose a coherent persona prompt from structured config."
- **A deterministic, zero-cost test double is an underserved niche.** Everyone testing an LLM agent wants reproducible, $0 replies in CI. The crowded space is *routers* (LiteLLM); a test double is not that, which is the whole point of the [RFC 0045 §H](0045-open-core-extraction-policy.md#h-the-candidate-set-and-the-per-extraction-rfc-requirement) "not another router" framing.
- **Config schemas are the welcome mat.** Draft-07 JSON schemas for "define your agent team in YAML" have funnel and documentation value and zero coupling — they are declarative data.

### M-2. One RFC, three repos — batching is correct here

[RFC 0045 §M-3](0045-open-core-extraction-policy.md#m-3-multiple-extraction-rfcs-will-inherit-the-same-rules) is explicit that a foundational policy exists so per-extraction RFCs stay small. For the flagship, the seam-cut (a pure-Python engine port, the in-process/remote backend design) was substantial enough to deserve its own RFC. For these three, the seam-cutting is near-zero: two are already leaf, the third has a single clean cut. Giving each its own RFC would triplicate the same inherited policy with nothing distinct to decide. So this RFC ratifies the **batch** as one decision, while keeping each artifact a **separate repo** ([§M-3](#m-3-the-rfc-0046-single-identity-precedent-applies-to-each)). This is the same judgment [RFC 0045 §H](0045-open-core-extraction-policy.md#h-the-candidate-set-and-the-per-extraction-rfc-requirement) anticipated when it labeled them "the low-coupling batch."

### M-3. The RFC 0046 single-identity precedent applies to each

[RFC 0046 §M-3](0046-budget-lease-extraction.md#m-3-it-is-a-library-not-a-framework--and-a-single-language-one) established that an extracted artifact must have one language and one identity — a Python developer cannot embed a Go engine, so the budget lease shipped as a Python library, not a polyglot bundle. The same discipline disambiguates each repo here:

- the prompt kit is **a Python package + bundled prompt data**, not "Python plus the persona runtime that uses it";
- the mock is **the protocol + the mock**, not the whole provider abstraction with its real-SDK dependencies;
- the schemas are **language-neutral data + a thin reference validator**, not a Python config framework — their audience writes configs in Go, Python, *and* Rust, so the artifact is the schema documents themselves.

## Goals

1. **Ratify the batch** as a single decision under [RFC 0045](0045-open-core-extraction-policy.md), with one repo per artifact ([§A](#a-the-batch-at-a-glance)).
2. **Make the prompt-composer seam-cut** — loader + renderer + data to MIT; the persona-state-coupled mixin stays BUSL and consumes them, with the MIT surface free of Persatrix-internal types ([§B](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut)).
3. **Extract the mock as a test double** — protocol + `MockProvider` — keeping the real providers and the routing factory BUSL ([§C](#c-persatrix-mock-llm--the-deterministic-test-double)).
4. **Extract the schemas as language-neutral data** + a reference validator, leaving the Go/Python/Rust consumers reading them in-tree under Option A ([§D](#d-persatrix-schemas--language-neutral-config-schemas), [§F](#f-sync-model-and-dogfooding)).
5. **Prove the dependency direction** for all three on the Python side ([§E](#e-dependency-direction-proof)).
6. **Preserve the safety invariants** of the injection-defense snippets and the loader's deny-by-default path rule in the standalone form ([Security](#security-considerations)).
7. **Refine RFC 0045 §H** to the single-identity artifacts ([§H](#h-relationship-to-rfc-0045-the-refinement)).

## Non-Goals

- **No code moves under this RFC.** Gated on [RFC 0045](0045-open-core-extraction-policy.md) Accepted *and* its boundary CI gate green ([Decision](#decision--next-steps)).
- **The persona runtime is not extracted.** `prompt_assembly.py` and the rest of `agents/persona_runtime/` stay BUSL ([§B](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut)). The kit is the reusable substrate the runtime sits on, not the runtime.
- **The real LLM providers are not extracted.** `llm_providers.py` (Anthropic/OpenAI), `llm_ollama.py`, and the `create_provider` factory in `llm_client.py` stay BUSL ([§C](#c-persatrix-mock-llm--the-deterministic-test-double)). Shipping those would be "another router" — explicitly rejected.
- **No schema redesign.** The schemas ship as-is; this RFC relicenses and republishes them, it does not change their shape.
- **No new memory, channel, or society capability is touched.** This is purely an extraction.
- **No Option-B flip now.** All three start monorepo-canonical ([§F](#f-sync-model-and-dogfooding)).

## Design / Implementation

### A. The batch at a glance

| Repo (MIT) | What ships | What stays BUSL | Seam-cut? |
|------------|-----------|-----------------|-----------|
| **`persatrix-prompts`** | `prompt_loader.py`, `persona_behavior.py`, `prompts/runtime/safety/**`, `prompts/runtime/persona/sections/**`, a thin context-dict composer | `prompt_assembly.py` (the persona-state-coupled mixin) + the rest of `persona_runtime` | **Yes** — the one real cut |
| **`persatrix-mock-llm`** | `llm_types.py` (the `LLMProvider` protocol + response types), `llm_offline.py` (`MockProvider`) | `llm_providers.py`, `llm_ollama.py`, `create_provider` factory | No — already leaf |
| **`persatrix-schemas`** | `schemas/*.json`, `blueprints/*/blueprint.yaml`, `templates/*.yaml`, `validate.py` (reference validator) | the Go planner, Python callers, Rust CLI that consume the schemas in-tree | No — declarative data |

### B. `persatrix-prompts` — the prompt kit (the one real seam-cut)

The current composition path is the `_PromptAssemblyMixin._build_system_prompt()` in `agents/persona_runtime/prompt_assembly.py`. It already delegates the two reusable jobs to leaf modules and keeps the Persatrix-specific assembly to itself. Grounding in the actual imports:

- `prompt_loader.py` imports only `functools`, `pathlib`, `typing`, and `yaml` — **zero project imports**. It loads safety snippets, persona section templates, and the behavioral-dimension YAML, enforcing a deny-by-default path-traversal rule.
- `persona_behavior.py` imports only `logging` and `from .prompt_loader import load_dimension_descriptions` — it depends on **nothing but the leaf loader**. It renders the five behavioral dimensions into natural-language bullets.
- `prompt_assembly.py`, by contrast, imports `..base.TaskInput`, `..observability.metrics`, `..persona_types.{AgentEvent, EventType}`, `..temporal.rendering`, *and* `..prompt_loader` + `..persona_behavior`. It is **coupled to the persona runtime** (agent events, OTEL instruments, the now-anchor clock, `PersonaState`).

**The cut:** the leaf pair (`prompt_loader` + `persona_behavior`) and the `prompts/runtime/` data move to `persatrix-prompts`. The composer mixin **stays BUSL** and re-points its `load_persona_section` / `load_snippet` / `render_behavior` imports at the MIT package. To give external users composition without dragging in the runtime, the MIT package additionally exposes a **framework-agnostic composer** that takes a plain context dict and returns the assembled prompt — but its surface is plain `str`/`dict`, never `PersonaState` or `AgentEvent`, satisfying the [RFC 0045 §A](0045-open-core-extraction-policy.md#a-the-three-tier-boundary) "generic surface" criterion. Persatrix's mixin keeps the persona-state-aware wiring (events, OTEL, now-anchor) on top of that core.

```
persatrix-prompts/               # MIT, Python
  persatrix_prompts/
    loader.py                    # from prompt_loader.py (deny-by-default; package-relative anchor)
    behavior.py                  # from persona_behavior.py (dimension renderer)
    compose.py                   # NEW: framework-agnostic compose(sections, context: dict) -> str
    data/
      safety/*.md                # injection defenses (delimiters, external-data envelope, discretion)
      persona/sections/*.md      # identity/background/behavior/quirks/goals/current-state templates
      persona/sections/behavior-dimensions.yaml
  examples/  LICENSE  NOTICE  CHANGELOG  CONTRIBUTING(DCO)  README
```

**Packaging seam.** `prompt_loader._default_repo_root()` anchors on the monorepo's `prompts/` subtree (`Path(__file__).parent.parent`). Standalone, the prompt data ships *inside* the package, so the default anchor switches to package-relative resource loading (`importlib.resources`). The public functions already accept an injectable `repo_root`, so this is a default-anchoring change, **not** an API change.

**Scope.** The kit ships `prompts/runtime/safety/` and `prompts/runtime/persona/sections/`. `prompts/runtime/task-agents/` (code-reviewer/code-writer/planner) is Persatrix-product-specific and stays BUSL ([Open Question 1](#open-questions)).

### C. `persatrix-mock-llm` — the deterministic test double

The provider abstraction already separates the contract from the implementations:

- `llm_types.py` is a **leaf by design** (its docstring records breaking the historical `llm_client ↔ llm_providers` cycle): it imports only `dataclasses`, `enum`, `typing`. It defines the `LLMProvider` Protocol and the normalized `LLMResponse`/`ToolCall`/`Usage`/`StopReason` types.
- `llm_offline.py` imports only stdlib + `yaml` + `from .llm_types import …`. It is the `MockProvider`: scripted, persona-flavored, deterministic, $0 replies that still emit synthetic token usage so cost/telemetry paths stay exercised. **No orchestrator/wallet/memory coupling.**

So the MIT artifact is the **protocol + the mock**, and nothing else moves. The real providers (`llm_providers.py`, `llm_ollama.py`) and the `create_provider` factory (in `llm_client.py`, which reads `PERSATRIX_OFFLINE`/config to *select* the mock) **stay BUSL** and continue to consume the MIT types and mock. Dependency direction: BUSL → MIT.

```
persatrix-mock-llm/              # MIT, Python
  persatrix_mock_llm/
    types.py                     # from llm_types.py — the LLMProvider Protocol + response types
    mock.py                      # from llm_offline.py — MockProvider
    adapters/
      adk.py                     # a BaseLlm test double (launch)
  examples/  LICENSE  NOTICE  CHANGELOG  CONTRIBUTING(DCO)  README
```

The positioning is the [RFC 0045 §H](0045-open-core-extraction-policy.md#h-the-candidate-set-and-the-per-extraction-rfc-requirement) "test your agents at $0 with a deterministic mock LLM" framing — a CI/testing tool, expressly **not** a routing library.

### D. `persatrix-schemas` — language-neutral config schemas

The schemas are declarative draft-07 JSON with zero code coupling and a `$id` already on the `persatrix.dev` namespace. The reference validator `validate.py` imports only stdlib + `jsonschema` + `yaml` — leaf. The schemas' real consumers are **multi-language**: the Go `internal/planner` validates workflows, the Python `validate.py` validates configs, the Rust CLI (`cli/src/validation.rs`) validates client-side. That multi-language audience is *why* the artifact is the schema documents, not a Python library ([§M-3](#m-3-the-rfc-0046-single-identity-precedent-applies-to-each)).

```
persatrix-schemas/               # MIT, language-neutral data + reference validator
  schemas/{agent,channel,workflow,optimization}.schema.json
  blueprints/{software-team,social-experiment}/blueprint.yaml   # examples
  templates/{personas,sub_agents}.yaml                          # examples
  validator/validate.py          # from validate.py — thin reference validator (convenience)
  LICENSE  NOTICE  CHANGELOG  CONTRIBUTING(DCO)  README
```

Under Option A ([§F](#f-sync-model-and-dogfooding)) the canonical copy stays in the monorepo and all three language consumers read it in-tree; the public repo is a generated mirror. So there is **no cross-language dependency-skew cost** while pre-1.0 — the mirror is consumed by external adopters, not by the Persatrix build.

### E. Dependency-direction proof

[RFC 0045 §H](0045-open-core-extraction-policy.md#h-the-candidate-set-and-the-per-extraction-rfc-requirement) requires an explicit proof the [§B invariant](0045-open-core-extraction-policy.md#b-the-dependency-direction-invariant) holds. All three MIT artifacts are Python-or-data, so the boundary to enforce is **MIT Python ↛ BUSL Python**:

| Unit | Upward imports into BUSL? | Evidence |
|------|---------------------------|----------|
| `loader.py` (from `prompt_loader.py`) | None | stdlib (`functools`, `pathlib`, `typing`) + `yaml` only — verified by source |
| `behavior.py` (from `persona_behavior.py`) | None | imports only `logging` + the leaf loader — verified |
| `compose.py` (new) | None | new MIT code; takes plain `dict`/`str`, depends only on `loader`/`behavior` |
| `types.py` (from `llm_types.py`) | None | stdlib only (`dataclasses`, `enum`, `typing`); leaf by design |
| `mock.py` (from `llm_offline.py`) | None | stdlib + `yaml` + `.types` only — verified |
| `validator/validate.py` (from `validate.py`) | None | stdlib + `jsonschema` + `yaml` only — verified |
| `schemas/**`, `blueprints/**`, `templates/**` | n/a (data) | JSON/YAML — no code |

**Mechanical enforcement.** [RFC 0045 §B](0045-open-core-extraction-policy.md#b-the-dependency-direction-invariant)'s Python `import-linter` contract already seeds two of these as forbidden-upward layers — "the prompt loader/safety snippets" and "the provider abstraction + mock." This RFC **extends that contract** to the full `persatrix_prompts`, `persatrix_mock_llm`, and the `validate` validator, and the proof is *that the gate is green with those packages added*, not a prose claim ([RFC 0046 §F](0046-budget-lease-extraction.md#f-dependency-direction-proof) precedent). **No Go deny-rule change is required** — the schemas are data, not Go code, and there is no MIT Go in this batch.

### F. Sync model and dogfooding

Per [RFC 0045 §D](0045-open-core-extraction-policy.md#d-source-of-truth-and-sync-model), all three repos start on **Option A — monorepo-canonical, mirror-out**, exactly as [RFC 0046 §G](0046-budget-lease-extraction.md#g-sync-model-and-dogfooding). Dogfooding is automatic and already true by construction:

- the persona runtime's composer consumes the kit's loader/renderer/compose;
- `create_provider` consumes the mock types + `MockProvider` (every `make demo-offline` run exercises it at $0);
- the Go planner, Python validator, and Rust CLI consume the schemas.

So the in-tree libraries are exercised in production by the BUSL core that depends on them — the public API is proven against a real consumer before any **Option-B flip**, which is recorded but deferred per repo, taken only once each API has stabilized and external contribution demand is real ([evolvable-over-back-compat](../development-workflow.md)).

### G. Adapters, per repo

The [RFC 0045 §G](0045-open-core-extraction-policy.md#g-repo-structure-core-plus-adapters) core-plus-adapters pattern applies **unevenly**, because only one of the three is a runtime participant:

- **`persatrix-mock-llm` gets real adapters.** A test double only helps if it drops into the host's model slot, so it ships a launch ADK `BaseLlm` adapter (and a LangChain `BaseChatModel` double is an obvious follow-up). Usage normalization is trivial here because the mock *produces* the usage object.
- **`persatrix-prompts` is data-first.** Its "integration" is reading strings into a system prompt — every framework already has a system-prompt slot — so it ships helpers/examples rather than runtime adapters. The framework-agnostic `compose()` is the closest thing to an adapter.
- **`persatrix-schemas` has no runtime adapters.** "Adapters" here are language-specific validators; the Python reference validator ships, and other languages validate against the published schema documents directly (the Go and Rust validators stay in the BUSL core).

### H. Relationship to RFC 0045 (the refinement)

[RFC 0045 §H](0045-open-core-extraction-policy.md#h-the-candidate-set-and-the-per-extraction-rfc-requirement) lists three composite candidate rows. As [RFC 0046 §I](0046-budget-lease-extraction.md#i-relationship-to-rfc-0045-the-refinement) did for the flagship, this RFC exercises the policy's per-extraction latitude and **narrows each row to its single-identity artifact**:

- **prompt-safety kit** — §H said "`prompts/runtime/safety/*` + persona section composer + `prompt_loader`." This RFC clarifies that the "composer" splits: the leaf renderer + loader + data go MIT, while the persona-state-coupled `prompt_assembly.py` stays BUSL ([§B](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut)).
- **agent-testing / mock provider** — §H said "`LLMProvider` protocol + `MockProvider`." Confirmed unchanged; the real providers + factory stay BUSL ([§C](#c-persatrix-mock-llm--the-deterministic-test-double)).
- **schemas + blueprints** — §H said "`schemas/*.json` + `blueprints/*.yaml` + validator." Confirmed; clarified as language-neutral data + a thin reference validator, consumed in-tree by three languages under Option A ([§D](#d-persatrix-schemas--language-neutral-config-schemas)).

The edit to RFC 0045 §H is applied alongside this RFC so the two stay consistent; no policy principle changes.

## Security Considerations

- **Injection-defense invariants must survive extraction.** The prompt kit is the safety-relevant artifact of this batch ([RFC 0045 §Security](0045-open-core-extraction-policy.md#security-considerations)): the `<|user_message|>` delimiter rule and the `<external_data source/flagged/sanitized>` envelope ([RFC 0009](0009-security-sandboxing.md), [RFC 0022](0022-persona-prompt-section-templating.md)) are *claims*, and a published kit that shipped them weakened would be worse than not shipping them. The exact snippet bytes and the tests asserting their presence in an assembled prompt move with the kit and run in standalone CI.
- **The loader's deny-by-default path rule is a security control, not a convenience.** `prompt_loader` rejects path separators, `..` traversals, absolute paths, and symlink escapes so a caller cannot reach outside the prompt subtree. The package-relative re-anchoring ([§B](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut)) must preserve this; the traversal tests move with it.
- **License-leak control.** The [§E](#e-dependency-direction-proof) `import-linter` gate is the primary control: any of these packages importing BUSL `agents/*` would distribute BUSL source under MIT on the next mirror. Merge-blocking, per [RFC 0045 §B](0045-open-core-extraction-policy.md#b-the-dependency-direction-invariant).
- **Schema strictness is a config-safety property.** The schemas' `additionalProperties: false` posture and footgun-rejecting patterns are part of what makes them safe to publish; the republished copies must be byte-faithful, and the validator's behavior is covered by tests.
- **No secrets, fixtures, or internal config leave the tree.** Each mirror carries only its library/data + tests + scaffolding — not `config/`, env files, or monorepo fixtures. The mock's scripted replies must be reviewed to contain no real prompts or internal data. Each repo regenerates its own third-party inventory from its own closure ([RFC 0045 §F](0045-open-core-extraction-policy.md#f-naming-versioning-and-release-conventions)).

## Phased Implementation Plan

Ordered, condition-gated steps — no calendar commitments; each gates the next on an *event*, mirroring [RFC 0045 §Phased Rollout](0045-open-core-extraction-policy.md#phased-rollout) and [RFC 0046](0046-budget-lease-extraction.md#phased-implementation-plan). After Phase 0 the three repos are largely independent and may proceed in parallel; the order below is by seam-cut risk.

### Phase 0: Prerequisites (owned by RFC 0045 and RFC 0046)

[RFC 0045](0045-open-core-extraction-policy.md) Accepted; its Python `import-linter` boundary gate merged green; and the [RFC 0046](0046-budget-lease-extraction.md) flagship pattern proven (mirror tooling, DCO scaffold, standalone-CI template established once and reused here). **Hard gate: nothing below begins until this holds.**

### Phase 1: `persatrix-prompts` (the seam-cut first)

Re-point `prompt_assembly.py`'s imports at the new package; move `prompt_loader.py` + `persona_behavior.py` + the `prompts/runtime/{safety,persona/sections}` data; add the package-relative loader anchor and the framework-agnostic `compose()`; carry the traversal-deny and snippet-presence tests. Stand up the repo skeleton + mirror + standalone CI with the boundary check. Deliverable: the kit usable standalone, with the BUSL composer consuming it.

### Phase 2: `persatrix-mock-llm`

Move `llm_types.py` + `llm_offline.py`; re-point `llm_client.py`/`llm_ollama.py` imports at the package; add the ADK `BaseLlm` adapter + usage-normalization/determinism tests. Confirm `make demo-offline` still runs at $0 against the library.

### Phase 3: `persatrix-schemas`

Move the schemas + example blueprints/templates + reference validator; wire the mirror; confirm the Go planner, Python validator, and Rust CLI still validate against the in-tree canonical copy. Welcome-mat README + schemastore.org publication is a follow-up, not a blocker.

### Phase 4: Option-A→B flips (deferred)

Considered per repo, gated on each API stabilizing + real external contribution demand ([§F](#f-sync-model-and-dogfooding)).

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| `persatrix-prompts` (MIT, mirrored) | `persatrix_prompts/loader.py`, `behavior.py`, `compose.py`, `data/safety/**`, `data/persona/sections/**`, scaffolding, CI | Created; `loader`/`behavior` recast from `agents/prompt_loader.py` + `agents/persona_behavior.py`; data from `prompts/runtime/` |
| `persatrix-mock-llm` (MIT, mirrored) | `persatrix_mock_llm/types.py`, `mock.py`, `adapters/adk.py`, scaffolding, CI | Created; recast from `agents/llm_types.py` + `agents/llm_offline.py` |
| `persatrix-schemas` (MIT, mirrored) | `schemas/*.json`, `blueprints/*/blueprint.yaml`, `templates/*.yaml`, `validator/validate.py`, scaffolding | Created; data from `schemas/`, `blueprints/`, `templates/`; validator from `agents/validate.py` |
| Persona runtime (BUSL) | `agents/persona_runtime/prompt_assembly.py` and import sites | Re-point `load_persona_section`/`load_snippet`/`render_behavior` at `persatrix_prompts`; mixin stays BUSL |
| LLM layer (BUSL) | `agents/llm_client.py`, `agents/llm_ollama.py` | Consume `persatrix_mock_llm` types + `MockProvider`; real providers + factory stay BUSL |
| Config validation (BUSL) | callers of `agents/validate.py`; Go `internal/planner`; Rust `cli/src/validation.rs` | Consume the in-tree canonical schemas (unchanged under Option A) |
| CI (monorepo) | Python `import-linter` contract | Extended to guard all three packages ([§E](#e-dependency-direction-proof)) |
| Policy | `docs/rfcs/0045-open-core-extraction-policy.md` §H | Refined to the single-identity artifacts ([§H](#h-relationship-to-rfc-0045-the-refinement)) |

## Test Strategy

- **Prompt kit**: traversal-deny tests (separators, `..`, absolute, symlink) move with the loader and run standalone; snippet-presence tests assert each safety snippet appears verbatim in an assembled prompt; the behavioral-dimension renderer keeps its shape-check and rendering tests; `compose()` gets context-dict assembly tests.
- **Mock**: determinism tests (same inputs → same scripted reply), synthetic-usage tests (cost/telemetry paths still see token counts), and the ADK adapter's drop-in test (the double satisfies `BaseLlm`).
- **Schemas**: the existing validator test corpus moves with `validate.py`; a fixture set of valid/invalid configs proves each schema; `additionalProperties:false` rejection cases are explicit.
- **Boundary tests**: the [§E](#e-dependency-direction-proof) `import-linter` gate is itself the proof — CI fails if any of the three packages gains an upward import into BUSL.
- **Dogfood regression**: the persona-runtime prompt tests, the `make demo-offline` $0 path, and the Go/Python/Rust schema-validation paths must all stay green after the re-pointing.

## Open Questions

1. **Task-agent prompts.** Whether `prompts/runtime/task-agents/*` (code-reviewer/code-writer/planner) is product-specific (stay BUSL, the [§B](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut) default) or generically useful enough to ship in the kit. Leaning BUSL; revisit on demand.
2. **Repo/package naming.** Branded `persatrix-prompts` / `persatrix-mock-llm` / `persatrix-schemas` per [RFC 0045 §F](0045-open-core-extraction-policy.md#f-naming-versioning-and-release-conventions). The mock and the schemas are the likeliest [§F](0045-open-core-extraction-policy.md#f-naming-versioning-and-release-conventions) override candidates (a neutral "deterministic mock LLM" or a vendor-neutral schema name may aid discoverability) — decided per repo only if adoption friction is shown to dominate the funnel benefit.
3. **Should the prompt kit lead on safety or composition?** The repo does both; the README/positioning emphasis (and therefore whether the name should be `persatrix-prompt-safety`) is a framing call folded into Question 2.
4. **Retire vs. thin-shim the moved Python modules.** Whether the migration deletes `agents/prompt_loader.py` / `llm_types.py` / `llm_offline.py` / `validate.py` outright or leaves deprecation shims is a per-phase mechanical detail under the [evolvable-over-back-compat](../development-workflow.md) stance (lean toward outright, pre-1.0).
5. **Blueprints/templates as `schemas` payload vs. their own future repo.** Shipped here as *examples* beside the schemas; a richer "blueprint gallery" could later warrant its own repo, but that would be fragment-cloud today.

## Decision / Next Steps

**Proposed decision:** extract the low-coupling batch as **three single-identity MIT repos** — `persatrix-prompts` (loader + behavior renderer + `prompts/runtime` data + a framework-agnostic composer, with the persona-state-coupled `prompt_assembly.py` kept BUSL and consuming it — [§B](#b-persatrix-prompts--the-prompt-kit-the-one-real-seam-cut)); `persatrix-mock-llm` (the `LLMProvider` protocol + `MockProvider`, with the real providers and factory kept BUSL — [§C](#c-persatrix-mock-llm--the-deterministic-test-double)); and `persatrix-schemas` (language-neutral schemas + example blueprints/templates + a thin reference validator, consumed in-tree by three languages under Option A — [§D](#d-persatrix-schemas--language-neutral-config-schemas)) — with the Python-side dependency-direction proof ([§E](#e-dependency-direction-proof)), Option A sync with automatic dogfooding ([§F](#f-sync-model-and-dogfooding)), per-repo adapters ([§G](#g-adapters-per-repo)), and the [§H](#h-relationship-to-rfc-0045-the-refinement) refinement of RFC 0045 §H.

**Hard prerequisites before any code moves** (Phase 0):

1. [RFC 0045](0045-open-core-extraction-policy.md) reaches **Accepted**.
2. Its MIT↛BUSL boundary CI gate (the Python `import-linter` contract) is merged green.
3. The [RFC 0046](0046-budget-lease-extraction.md) flagship pattern is proven, so the mirror tooling / DCO scaffold / standalone-CI template are established and reused here.

**On those holding**, execute Phase 1 (`persatrix-prompts`, the one seam-cut) first; the mock and schemas follow and may proceed in parallel.

**Sequence (ordered, no timelines):** RFC 0045 (policy) → RFC 0046 (budget-lease flagship) → **this RFC (low-coupling batch)**. The commercial-architecture (Private) RFC remains named-but-deferred until a forcing function ([RFC 0045 §C](0045-open-core-extraction-policy.md#c-the-private-tier-reserved-seams-and-the-no-retraction-rule)).

## Related Documentation

- [RFC 0045 — Open-Core Library Extraction Policy](0045-open-core-extraction-policy.md) — the governing three-tier policy this RFC inherits and refines in [§H](#h-relationship-to-rfc-0045-the-refinement)
- [RFC 0046 — Budget-Lease Library Extraction](0046-budget-lease-extraction.md) — the flagship whose single-identity / audience→artifact precedent this RFC follows
- [RFC 0022 — Persona Prompt Section Templating](0022-persona-prompt-section-templating.md) — the templating contract the prompt kit implements
- [RFC 0009 — Security & Sandboxing](0009-security-sandboxing.md) — the external-data envelope and injection-defense posture whose invariants must survive extraction
- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — adjacent to the provider/mock split
- [LICENSE](../../LICENSE), [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md), [NOTICE](../../NOTICE) — the BUSL terms and attribution conventions each extracted MIT repo re-bases
- [development-workflow.md](../development-workflow.md), [BRANCHING.md](../BRANCHING.md) — the evolvable-over-back-compat stance behind the Option-A default and the retire-vs-shim choices
- [RFC README](README.md) — RFC process, reserved numbers, and format
