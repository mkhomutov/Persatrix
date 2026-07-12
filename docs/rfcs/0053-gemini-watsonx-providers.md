---
# Allowed values are documented in README.md. The YAML front-matter is the
# source of truth read by `scripts/rfcs.py` to regenerate INDEX.md — keep
# it in sync with the bold-markdown header below (which is what GitHub
# renders for human readers).
id: RFC-0053
title: "Gemini and watsonx.ai LLM Providers"
summary: "Add Google Gemini and IBM watsonx.ai as first-class LLM providers — the second concrete dogfood of the RFC 0033 §H multi-provider extensibility seam (one provider class + one factory branch + priced alias entries each). Brings the configurable provider roster to four cloud vendors (Anthropic, OpenAI, Gemini, watsonx.ai) plus local Ollama and the offline mock, which is what makes the RFC 0052 four-vendor human-free brainstorm demo possible."
type: feature
status: implementing
author: Maksim Khomutov
created: 2026-06-28
target: "v0.3.11"
depends_on:
  - RFC-0033
  - RFC-0023
  - RFC-0004
---

# RFC 0053 — Gemini and watsonx.ai LLM Providers

**Type**: feature
**Status**: 🚧 Implementing (v0.3.11 — bundled with [RFC 0052](0052-autonomous-agent-channels.md); [plan](../v0.3.11-plan.md), [PR plan](0053-pr-plan.md))
**Author**: Maksim Khomutov
**Date**: 2026-06-28
**Target**: v0.3.11 (bundled with [RFC 0052](0052-autonomous-agent-channels.md), independently shippable/cuttable; sequenced by [v0.3.x-sequencing Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone), pinned at [plan opening](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28))
**Depends on**: RFC 0033 (Provider-Agnostic Model Alias Layer — the extension seam), RFC 0023 (LLM Call Leasing — non-local providers must be priced), RFC 0004 (the `LLMProvider` Protocol)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The extension recipe (RFC 0033 §H)](#a-the-extension-recipe-rfc-0033-h)
  - [B. Gemini provider](#b-gemini-provider)
  - [C. watsonx.ai provider](#c-watsonxai-provider)
  - [D. Pricing — the missing-price guard](#d-pricing--the-missing-price-guard)
  - [E. Demo + compose parity](#e-demo--compose-parity)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Add **Google Gemini** and **IBM watsonx.ai** as first-class LLM providers, selectable the same config-driven way as `anthropic` / `openai` / `ollama` / `mock`. This is the **second concrete dogfood of the [RFC 0033 §H multi-provider extensibility](0033-model-alias-layer.md#h-multi-provider-extensibility) seam** — which already names Gemini among its candidate providers — so each provider is *one new class + one `create_provider` branch + N priced alias entries*, with no heuristic-table or routing-rule changes.

The motivating consumer is the [RFC 0052](0052-autonomous-agent-channels.md) autonomous channel: with four cloud vendors configurable, the flagship demo becomes **Anthropic + OpenAI + Gemini + watsonx.ai personas brainstorming in one channel with no human** — a striking cross-vendor showcase that the provider-agnostic architecture (RFC 0033) was built to enable.

## Motivation

RFC 0033 decoupled agent configs from vendor IDs precisely so that "add a provider" is a one-file change, and [§H](0033-model-alias-layer.md#h-multi-provider-extensibility) documented the recipe but left it unexercised beyond the OpenAI/Ollama/mock set already in tree. Two pulls make Gemini and watsonx.ai the right next providers:

1. **Cross-vendor reach.** Gemini (Google) and watsonx.ai (IBM, including the Llama / Granite / Mistral models it hosts) are two of the most-requested provider families not yet supported. Adding them widens who can run Persatrix on the model they already pay for — the same adoption axis as v0.3.4 ("Any Model, Any Provider").
2. **The cross-provider demo.** A human-free channel where four *different vendors'* models brainstorm together is the most vivid possible proof that the conversation layer is provider-agnostic. It is the headline manual test and demo for the next version, and it is impossible today because only two cloud vendors are wired.

Doing nothing leaves the §H seam documented-but-unproven beyond the original set and blocks the four-vendor demo.

## Goals

1. **Gemini as a first-class provider** — an agent/alias declaring `provider: gemini` routes to a `GeminiProvider` implementing the existing `LLMProvider` Protocol, with tool-calling mapped to the protocol's tool-round shape.
2. **watsonx.ai as a first-class provider** — `provider: watsonx` routes to a `WatsonxProvider`, with the IBM-specific `project_id` + regional endpoint carried through `provider_config` (the same mechanism OpenAI's `base_url` uses, RFC 0033 §D rule 2).
3. **Priced by default** — both ship demo alias configs with explicit per-token pricing, so the missing-price guard and the derived Go cost table ([RFC 0033 §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias), shipped v0.3.4) keep the RFC 0023 budget gate live. No silent $0.
4. **Demo + compose parity** — `make demo-gemini` and `make demo-watsonx` join the existing `demo-*` family, each mounting a per-provider alias config; matching `docker-compose.gemini.yaml` / `docker-compose.watsonx.yaml`.
5. **Enable the RFC 0052 four-vendor brainstorm** — the providers are usable in an autonomous channel where four personas each run on a different vendor (the consuming demo + MT live in [RFC 0052](0052-autonomous-agent-channels.md), not here).

## Non-Goals

- **No change to the alias/resolver contract.** RFC 0033 is the seam; this RFC only adds entries to the `provider` dispatch. No new routing surface, no heuristic table.
- **Not an exhaustive provider sweep.** Mistral-direct, Cohere, Bedrock, Vertex-via-service-account, etc. are out of scope — they follow the same §H recipe later if demanded.
- **No provider-specific feature surfacing** beyond what the `LLMProvider` Protocol already exposes (message create + tool rounds + usage). Gemini "thinking" config and watsonx model-tuning parameters are not plumbed as first-class knobs in this RFC (notedeferred — see [OQ #3](#open-questions)).
- **Not changing any default provider.** Per the v0.3.4 "no default provider" stance, these ship UNCONFIGURED; an operator selects them explicitly (run a demo, or set the alias).

## Design / Implementation

### A. The extension recipe (RFC 0033 §H)

Each provider is exactly three edits, per the documented seam:

1. A provider class in `agents/` implementing `LLMProvider` from [`agents/llm_types.py`](../../agents/llm_types.py) — `name`, `async create_message(...)`, `format_tool_definitions(tools)`, `append_tool_round(...)` — mirroring `AnthropicProvider` / `OpenAIProvider` in [`agents/llm_providers.py`](../../agents/llm_providers.py).
2. A `provider == "gemini"` / `provider == "watsonx"` branch in [`create_provider`](../../agents/llm_factory.py) that reads the API key from the environment, threads `provider_config`, and surfaces a clear install hint on missing SDK (the existing `ImportError → SystemExit` pattern).
3. Priced alias entries (demo configs + docs) pointing at the new providers.

No `_infer_provider` extension exists to touch — it was retired in RFC 0033 Phase 3.

### B. Gemini provider

- **SDK / wire.** Default to the native **`google-genai`** SDK (`GeminiProvider`), so pricing, telemetry (`persatrix.llm.model_alias`), and native function-calling are clean and attributed to a distinct `gemini` provider rather than masquerading as `openai`. (Gemini also exposes an OpenAI-compatible endpoint, which would let `OpenAIProvider` + `base_url` cover it with zero new code — see [OQ #1](#open-questions); the default is the first-class class.)
- **Auth.** `GEMINI_API_KEY` (fallback `GOOGLE_API_KEY`), read in the factory branch; startup warning when unset (the existing S-09 pattern for non-local providers).
- **Models / aliases.** Demo aliases `quality` / `fast` / `summarizer` → `gemini-3.5-flash`. (Originally `quality → gemini-2.5-pro`, `fast`/`summarizer → gemini-2.5-flash`; Google retired both for new API users on 2026-07-12, so the demo config was repointed to `gemini-3.5-flash`.)
- **Tool calls.** Map the protocol's tool definitions to Gemini `function_declarations` and tool results back into the `append_tool_round` shape.
- **`provider_config`.** Optional (e.g. a Vertex `project`/`location` if an operator routes through Vertex); empty for the default Gemini-API path.

### C. watsonx.ai provider

- **SDK / wire.** Native **`ibm-watsonx-ai`** SDK (`WatsonxProvider`) over the chat/foundation-model inference endpoint. watsonx has no broad OpenAI-compatible surface, so a native class is required (not a `base_url` reuse).
- **Auth + required `provider_config`.** IBM Cloud IAM key `WATSONX_API_KEY` from the environment, plus two **required** `provider_config` fields the client cannot run without: `project_id` (or `space_id`) and the regional `url` (e.g. `https://us-south.ml.cloud.ibm.com`). The factory branch validates their presence and **fails closed at construction** with an actionable message — like the missing-*SDK* pattern, **not** the softer missing-*key* warning (which only logs and defers the failure to first request, S-09). The distinction is deliberate: an API key absence is recoverable per-request, but required config the client literally cannot construct without should fail loud at startup. These two fields are **config, not secrets**, so they live in the alias `provider_config` (the single source of truth the factory reads), exactly the channel OpenAI's `base_url` uses — *not* the env path the secret key takes.
  > **Amendment (post-PR 2).** Because `project_id`/`url` are non-secret, each also accepts a `WATSONX_*` **env fallback** (`WATSONX_PROJECT_ID` / `WATSONX_SPACE_ID` / `WATSONX_URL`), resolved by `resolve_watsonx_config` with Ollama's `base_url` precedence (`provider_config` → env → default). `provider_config` remains the source of truth (it wins when set); the env channel exists so the shipped demo config stays generic and an operator's project id need not be committed to VCS. Only `WATSONX_API_KEY` is still env-*only* (the sole secret). `url` now carries a us-south default, so **only** a missing `project_id`/`space_id` fails closed. This relaxes the original "*not* the env path" wording for these non-secret fields; the secret/non-secret boundary is unchanged.
- **Models / aliases.** The alias `model:` carries the watsonx model id verbatim — e.g. `quality → meta-llama/llama-3-3-70b-instruct`, `fast → ibm/granite-3-8b-instruct`. (Exact ids/pricing are operator-set per the current watsonx catalog.)
- **Tool calls.** watsonx's chat API supports `tools` for tool-capable models; map the protocol round to it. Models without native tool support degrade to no-tool turns — noted as a per-model constraint in the provider docstring, not a blocker for the brainstorm demo (which is conversation, not tool use).

### D. Pricing — the missing-price guard

Gemini and watsonx.ai are **non-local** providers, so the [RFC 0033 missing-price guard](0033-model-alias-layer.md#f-pricing-keyed-by-alias) (shipped v0.3.4) fails closed on any **unpriced** alias (an unpriced non-local model would read $0 in the derived Go cost table and silently disable the RFC 0023 budget gate). Every demo alias entry therefore carries explicit `input_per_1m_tokens` / `output_per_1m_tokens` set to the vendor's current published price. The numbers are a config value the operator keeps current, not a code constant — the RFC fixes the *requirement*, not a frozen rate. **One honest limit:** the guard checks price *presence*, not *accuracy* — a stale or wrong rate still passes and silently mis-budgets. That is why the autonomous-channel mandatory cost cap (RFC 0052 Goal #4) is a genuine *second* bound rather than a redundant one: the priced lease meters spend (and can drift if a rate goes stale), while the per-interaction cap bounds spend regardless of per-token accuracy. A four-vendor unattended brainstorm is bounded by the cap even if a vendor's price drifts — the two are complementary, not the same guarantee twice.

### E. Demo + compose parity

`make demo-gemini` / `make demo-watsonx` mount `config/demo/gemini/optimization.yaml` / `config/demo/watsonx/optimization.yaml` (priced aliases pointing the society at the new provider), exactly like `demo-openai`. The env-vs-`provider_config` split follows the secret/config line from [§B](#b-gemini-provider)/[§C](#c-watsonxai-provider): the matching `docker-compose.gemini.yaml` / `docker-compose.watsonx.yaml` pass only the **secret keys** as env (`GEMINI_API_KEY`, fallback `GOOGLE_API_KEY`; `WATSONX_API_KEY`), while watsonx's **non-secret** `project_id`/`url` resolve from the mounted alias `provider_config` **or** a `WATSONX_*` env fallback the overlay plumbs (see the §C amendment) — the demo config ships generic and the operator sets `WATSONX_PROJECT_ID` in `.env`, so their project id never lands in VCS. The **four-vendor** autonomous brainstorm demo is a separate, RFC 0052-owned target that references all four cloud alias configs at once.

## Security Considerations

- **New credential surfaces.** Two more API-key env vars (`GEMINI_API_KEY` / `GOOGLE_API_KEY`, `WATSONX_API_KEY`). watsonx's `project_id`/`url` are **config, not credentials**, and flow through the alias `provider_config` ([§C](#c-watsonxai-provider)), not the secret path. The keys flow only through the existing env → provider path and must be covered by the RFC 0009 secret redactor's patterns (add Google/IBM key shapes to the redactor allow-list so they never reach logs).
- **Cost.** Both are real-spend providers; the missing-price guard ([§D](#d-pricing--the-missing-price-guard)) keeps the budget gate live, and the RFC 0052 demo runs them under the mandatory per-interaction cap. No new uncapped path.
- **No new ingestion path.** These are outbound LLM calls over the existing provider abstraction; they add no inbound surface and no change to the RFC 0009 `<external_data>` envelope.
- **SDK supply chain.** Two new optional dependencies (`google-genai`, `ibm-watsonx-ai`); both are optional installs surfaced via the existing `ImportError → SystemExit` install hint, so a deployment that doesn't use them carries no new runtime dependency.

## Phased Implementation Plan

### Phase 1: Gemini provider

`GeminiProvider` (native `google-genai`) + factory branch + tool-round mapping + `config/demo/gemini/` + `make demo-gemini` + `docker-compose.gemini.yaml` + redactor patterns + docs. Acceptance: a society configured on `gemini` completes a task and a DM turn; cost attributes to priced aliases.

### Phase 2: watsonx.ai provider

`WatsonxProvider` (native `ibm-watsonx-ai`) + factory branch (with required `project_id`/`url` validation) + tool-round mapping (tool-capable models) + `config/demo/watsonx/` + `make demo-watsonx` + `docker-compose.watsonx.yaml` + redactor patterns + docs. Acceptance: same bar as Phase 1.

### Phase 3: Four-vendor enablement (handoff to RFC 0052)

**Config-only — no new provider code.** Once Phases 1–2 land, pinning distinct personas to `anthropic` / `openai` / `gemini` / `watsonx` aliases in one channel is pure RFC 0033 alias config; this "phase" is the demo blueprint that does so, not a code phase, and could equally be counted as RFC 0052 work. RFC 0053's contribution ends when both providers are individually usable. The autonomous demo target + MT that *exercise* the cross-vendor roster live in [RFC 0052 Phase 4](0052-autonomous-agent-channels.md#phase-4-flagship-demo) and own that work.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/llm_gemini.py`, `agents/llm_watsonx.py` (new); `agents/llm_factory.py` | Two `LLMProvider` classes + two `create_provider` branches |
| Python agents | `agents/requirements.txt` (or equiv) | `google-genai`, `ibm-watsonx-ai` as optional deps |
| Go orchestrator | `internal/security/` redactor patterns | Google / IBM key shapes (no cost-table change — it derives from the alias map) |
| Config | `config/demo/gemini/optimization.yaml`, `config/demo/watsonx/optimization.yaml`; alias-schema docs | Priced aliases for each provider |
| Infra | `docker-compose.gemini.yaml`, `docker-compose.watsonx.yaml`; `Makefile` | `demo-gemini` / `demo-watsonx` targets + compose parity |
| Docs | `docs/guides/model-providers.md`; `docs/manual-tests/` | Provider setup guide entries; per-provider smoke MTs |

The Go cost table needs no per-provider code — it is derived from `models.aliases` (RFC 0033 §F), so a priced alias entry is all the Go side needs.

## Test Strategy

- **Unit tests**: each provider maps a tool round-trip correctly and reports `Usage`; the watsonx factory branch fails closed when `project_id`/`url` are absent; both warn (not crash) on a missing key at startup.
- **Integration tests**: a society on `gemini` and a society on `watsonx` each complete a task + a persona turn against a recorded/mock transport; cost attributes to the priced alias.
- **E2E / smoke tests**: `make demo-gemini` / `make demo-watsonx` run a short society (live, keyed) and produce non-empty output; the offline path maps both to mock so CI needs no keys.
- **Manual tests**: `MT-PROVIDER-GEMINI-001`, `MT-PROVIDER-WATSONX-001` (single-provider smoke, live). The **cross-vendor** `MT-AUTONOMOUS-MULTIPROVIDER-001` (four vendors brainstorm with no human) lives in [RFC 0052](0052-autonomous-agent-channels.md#test-strategy) and depends on both providers landing here.

## Open Questions

> **Status — resolved 2026-06-28 at [v0.3.11 plan opening](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28).** **#1 → native `google-genai`** (the documented default); **#4 → ship the SDKs as extras**; **#2 → watsonx model/region calibrated at PR time**; **#3 → provider-native knobs out of scope**. Detail in the [RFC 0053 PR plan](0053-pr-plan.md#open-question-resolutions-locked-at-plan-authoring-time).

1. **Gemini via native SDK or the OpenAI-compatible endpoint?** Native `google-genai` is the default (clean provider identity, native tool-calling, distinct pricing/telemetry). The OpenAI-compat endpoint would need *zero* new code (`OpenAIProvider` + `base_url`) but files Gemini traffic under `openai` for cost/telemetry and forfeits native features. Lean **native**; revisit if the native SDK proves heavy. — **Resolved (v0.3.11): native `google-genai`** (first-class `GeminiProvider`; the OpenAI-compat path is the documented fallback if the SDK proves heavy).
2. **watsonx model + region defaults for the demo.** Which hosted model is the demo `quality` alias (a Llama-3.3-70B vs Granite vs Mistral-Large), and which region URL ships as the example? Pick a broadly-available default; document how to change it. Calibrate at PR time against the current watsonx catalog.
3. **Surface provider-native knobs (Gemini thinking budget, watsonx decoding params)?** Out of scope here (the Protocol doesn't model them). If the RFC 0052 brainstorm benefits from Gemini "thinking," that pairs naturally with the deferred RFC 0051 Phase 4 (`depth: deep` native extended thinking) rather than this RFC.
4. **Dependency packaging.** Ship `google-genai` / `ibm-watsonx-ai` as extras (`pip install persatrix[gemini,watsonx]`) vs. base requirements? Lean **extras** so a single-provider deployment stays lean — consistent with the optional-install `ImportError` hint pattern.

## Decision / Next Steps

**Status**: 🚧 Implementing (v0.3.11). Bundled with [RFC 0052](0052-autonomous-agent-channels.md) into v0.3.11 per the ratified [v0.3.x-sequencing Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone) ([#709](https://github.com/mkhomutov/Persatrix/pull/709)); independently shippable/cuttable per the [v0.3.11 plan](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28).

Done at plan opening:

1. ✅ [`docs/rfcs/0053-pr-plan.md`](0053-pr-plan.md) — Phase 1 (Gemini) then Phase 2 (watsonx), each one provider class + factory branch + demo config + compose + redactor patterns + docs; Phase 3 is the extras/closeout + handoff.
2. ✅ [OQ #1](#open-questions) (Gemini native vs OpenAI-compat) resolved — **native** `google-genai`; see the §Status note above.
3. Hand off Phase 3 (four-vendor enablement) to [RFC 0052 Phase 4](0052-autonomous-agent-channels.md#phase-4-flagship-demo) once both providers land ([RFC 0052 PR 9](0052-pr-plan.md#pr-9-featurev0311-rfc0052-demo-multivendor--phase-4b-four-vendor-headline--closeout-cuttable)).

## Related Documentation

- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — the extension seam ([§H](0033-model-alias-layer.md#h-multi-provider-extensibility)) this RFC dogfoods; pricing ([§F](0033-model-alias-layer.md#f-pricing-keyed-by-alias)) and factory integration ([§D](0033-model-alias-layer.md#d-factory-integration)).
- [RFC 0052 — Autonomous Agent-Only Channels](0052-autonomous-agent-channels.md) — the consuming demo: a four-vendor human-free brainstorm; the cross-vendor MT lives there.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — why non-local providers must be priced (the budget gate).
- [v0.3.x Sequencing — Amendment 2026-06-28](../v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone) — bundles this RFC with RFC 0052 into the next version.
- [docs/guides/model-providers.md](../guides/model-providers.md) — the operator-facing provider setup guide this extends.
