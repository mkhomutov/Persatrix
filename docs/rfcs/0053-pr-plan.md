# RFC 0053 — PR Implementation Plan (Phases 1–3 — v0.3.11 scope, bundled with RFC 0052)

**RFC**: [0053-gemini-watsonx-providers.md](0053-gemini-watsonx-providers.md)
**Created**: 2026-06-28
**Branch prefix**: `feature/v0311-rfc0053-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.11-plan.md Phase 1 — Implement RFC 0052 + RFC 0053](../v0.3.11-plan.md#phase-1--implement-rfc-0052--rfc-0053)

---

## Overview

RFC 0053 adds **Google Gemini** and **IBM watsonx.ai** as first-class LLM providers, the second concrete dogfood of the [RFC 0033 §H](0033-model-alias-layer.md#h-multi-provider-extensibility) multi-provider extensibility seam — each is *one new class + one `create_provider` branch + priced alias entries*, no heuristic-table or routing-rule changes. Its motivating consumer is the [RFC 0052](0052-autonomous-agent-channels.md) flagship demo: a **four-vendor human-free brainstorm** (Anthropic + OpenAI + Gemini + watsonx.ai), impossible today with only two cloud vendors wired.

This plan covers all three phases across **3 PRs**, mirroring the RFC's [phasing](0053-gemini-watsonx-providers.md#phased-implementation-plan):

- **Phase 1 — Gemini (PR 1).** `GeminiProvider` on the native `google-genai` SDK + factory branch + tool-round mapping + `config/demo/gemini/` + `make demo-gemini` + `docker-compose.gemini.yaml` + redactor patterns + docs.
- **Phase 2 — watsonx.ai (PR 2).** `WatsonxProvider` on the native `ibm-watsonx-ai` SDK + factory branch (with **required** `project_id`/`url` validation, fail-closed at construction) + tool-round mapping + `config/demo/watsonx/` + `make demo-watsonx` + `docker-compose.watsonx.yaml` + redactor patterns + docs.
- **Phase 3 — four-vendor handoff + closeout (PR 3).** Config-only — no new provider code. The cross-vendor blueprint + MT live in [RFC 0052 PR 8](0052-pr-plan.md#pr-8-featurev0311-rfc0052-demo-multivendor--phase-4b-four-vendor-headline--closeout-cuttable); this PR finalizes the extras packaging + the model-providers guide and flips RFC 0053 to ✅ Implemented.

**Hard prerequisites (all shipped):** RFC 0033 alias layer + §F alias-keyed pricing + §H extension seam (v0.3.4 ✅), RFC 0023 leasing — non-local providers must be priced (v0.3.2 ✅), RFC 0004 the `LLMProvider` Protocol (✅).

**Bundled but independently shippable.** RFC 0052 runs on any single provider, so RFC 0053 is **cuttable** from v0.3.11 — if a provider SDK proves fiddly, the four-vendor headline slips a point release and RFC 0052 ships with the offline/two-vendor demo ([master plan §Risk](../v0.3.11-plan.md#risk-and-mitigations)). The two providers are also independent of *each other*: PR 1 (Gemini) and PR 2 (watsonx) can land in either order or one without the other.

### Open-question resolutions locked at plan-authoring time

- **[OQ #1](0053-gemini-watsonx-providers.md#open-questions) — Gemini via the native `google-genai` SDK** (first-class `GeminiProvider`), **not** the OpenAI-compat endpoint. Clean provider identity (`provider: gemini`), native function-calling, distinct pricing/telemetry — the traffic is filed under `gemini`, not masquerading as `openai`. (The zero-code OpenAI-compat path is documented as the fallback if the native SDK proves heavy.)
- **[OQ #4](0053-gemini-watsonx-providers.md#open-questions) — ship the SDKs as extras** (`pip install persatrix[gemini,watsonx]` in [`agents/pyproject.toml`](../../agents/pyproject.toml)), consistent with the optional-install `ImportError → SystemExit` hint pattern, so a single-provider deployment stays lean.
- **[OQ #2](0053-gemini-watsonx-providers.md#open-questions) — watsonx demo model + region** calibrated at PR 2 time against the live watsonx catalog (a broadly-available default `quality`/`fast` alias + an example region URL, documented as operator-overridable).
- **[OQ #3](0053-gemini-watsonx-providers.md#open-questions) — provider-native knobs (Gemini thinking budget, watsonx decoding params) out of scope** — the Protocol doesn't model them; pairs with the deferred RFC 0051 Phase 4, not this RFC.

### File-size constraints (verified at plan authoring, cap = 500 per [`file_size.py --strict`](../../scripts/checks/file_size.py))

| File | Lines | Headroom | Routing |
|------|-------|----------|---------|
| New `agents/llm_gemini.py`, `agents/llm_watsonx.py` | — | — | Each provider is its **own module** (the RFC §Files-Touched routing), so [`llm_providers.py`](../../agents/llm_providers.py) (287) stays lean. |
| [`agents/llm_factory.py`](../../agents/llm_factory.py) | 171 | ample | Two `create_provider` branches fit. |
| [`internal/security/redactor.go`](../../internal/security/redactor.go) | — | — | Google / IBM key-shape patterns join the existing allow-list (the RFC 0009 secret-redactor seam). |

---

## Dependency Graph

```
RFC 0033 §H seam + §F pricing + RFC 0023 lease (all shipped)        ← HARD PREREQUISITES
   │
   ├── PR 1 (Phase 1: GeminiProvider [native google-genai] + factory branch + tool-round mapping
   │     │   + config/demo/gemini/ + make demo-gemini + docker-compose.gemini.yaml
   │     │   + GEMINI_API_KEY/GOOGLE_API_KEY redactor patterns + docs + MT-PROVIDER-GEMINI-001)
   │     │                                                                                   ┐
   ├── PR 2 (Phase 2: WatsonxProvider [native ibm-watsonx-ai] + factory branch with REQUIRED   │
   │     │   project_id/url validation [fail-closed at construction] + tool-round mapping       │ both
   │     │   + config/demo/watsonx/ + make demo-watsonx + docker-compose.watsonx.yaml           │ feed
   │     │   + WATSONX_API_KEY redactor patterns + docs + MT-PROVIDER-WATSONX-001)              │ 0052
   │     │      (independent of PR 1 — either order)                                            │ PR 8
   │     ↓                                                                                       ┘
   └── PR 3 (Phase 3: extras packaging [persatrix[gemini,watsonx]] + model-providers guide
             finalize + RFC closeout; the four-vendor blueprint + MT live in RFC 0052 PR 8)
```

---

## PR Sequence

### PR 1: `feature/v0311-rfc0053-gemini` — Phase 1: Gemini provider (native)

**Depends on**: RFC 0033 §H seam (shipped).
**Purpose**: A first-class `GeminiProvider` on the native `google-genai` SDK, selectable via `provider: gemini`.

#### Scope

| File | Change |
|------|--------|
| New `agents/llm_gemini.py` | `GeminiProvider` implementing the [`LLMProvider`](../../agents/llm_types.py) Protocol (`name`, `create_message`, `format_tool_definitions`, `append_tool_round`); map the protocol's tool definitions to Gemini `function_declarations` and results back into `append_tool_round`. |
| [`agents/llm_factory.py`](../../agents/llm_factory.py) | A `provider == "gemini"` branch reading `GEMINI_API_KEY` (fallback `GOOGLE_API_KEY`) from the environment, threading optional `provider_config` (Vertex `project`/`location` if routed via Vertex; empty for the default Gemini-API path), with the `ImportError → SystemExit` install hint on missing SDK; startup warning when the key is unset (the S-09 pattern). |
| [`agents/pyproject.toml`](../../agents/pyproject.toml) | `google-genai` as an **extra** ([OQ #4](0053-gemini-watsonx-providers.md#open-questions)). |
| `config/demo/gemini/optimization.yaml` | **Priced** demo aliases (`quality → gemini-2.5-pro`, `fast → gemini-2.5-flash`, `summarizer → gemini-2.5-flash`) with explicit `input_per_1m_tokens` / `output_per_1m_tokens` — the missing-price guard fails closed on an unpriced non-local alias ([RFC §D](0053-gemini-watsonx-providers.md#d-pricing--the-missing-price-guard)). |
| `Makefile` + `docker-compose.gemini.yaml` | `make demo-gemini` mounting the priced alias config; compose passes only the **secret key** as env. |
| [`internal/security/redactor.go`](../../internal/security/redactor.go) | Google key-shape patterns added to the redactor allow-list (keys never reach logs). |
| [`docs/guides/model-providers.md`](../../docs/guides/model-providers.md) + `docs/manual-tests/` | Setup entry; `MT-PROVIDER-GEMINI-001` (single-provider smoke, live). |

#### Tests

- Tool round-trip maps correctly + reports `Usage`; warns (not crashes) on a missing key at startup.
- A society on `gemini` completes a task + a DM turn against a recorded/mock transport; cost attributes to the priced alias.

#### PR checklist

- [ ] `pytest agents/tests/test_llm_gemini.py -q`; `ruff`/`mypy` clean; `go test ./internal/security/...`.
- [ ] Missing-price guard verified — an unpriced Gemini alias fails closed.
- [ ] `make demo-gemini` boots; offline path maps to mock (CI needs no key).
- [ ] RFC 0053 Master-Index row `🚧 Implementing` already applied by the planning PR.

---

### PR 2: `feature/v0311-rfc0053-watsonx` — Phase 2: watsonx.ai provider (native)

**Depends on**: RFC 0033 §H seam (shipped). Independent of PR 1.
**Purpose**: A first-class `WatsonxProvider` on the native `ibm-watsonx-ai` SDK, selectable via `provider: watsonx`.

#### Scope

| File | Change |
|------|--------|
| New `agents/llm_watsonx.py` | `WatsonxProvider` over the chat/foundation-model inference endpoint (watsonx has no broad OpenAI-compatible surface, so a native class is required). Map the protocol tool round to watsonx `tools` for tool-capable models; models without native tool support degrade to no-tool turns (a per-model docstring constraint, not a blocker for the conversation demo). |
| [`agents/llm_factory.py`](../../agents/llm_factory.py) | A `provider == "watsonx"` branch reading `WATSONX_API_KEY` from the environment, **plus required `provider_config` fields `project_id` (or `space_id`) and the regional `url`** — the factory **fails closed at construction** with an actionable message when they are absent (the missing-*SDK* pattern, **not** the softer missing-*key* warning: required config the client cannot construct without should fail loud at startup). These two are **config, not secrets**, so they live in the alias `provider_config` (the single source of truth the factory reads), the same channel OpenAI's `base_url` uses — *not* the env path. |
| [`agents/pyproject.toml`](../../agents/pyproject.toml) | `ibm-watsonx-ai` as an **extra**. |
| `config/demo/watsonx/optimization.yaml` | **Priced** demo aliases carrying the watsonx model id verbatim (e.g. `quality → meta-llama/llama-3-3-70b-instruct`, `fast → ibm/granite-3-8b-instruct`); exact ids/pricing/region calibrated at PR time ([OQ #2](0053-gemini-watsonx-providers.md#open-questions)); an example `project_id`/`url` the operator fills in. |
| `Makefile` + `docker-compose.watsonx.yaml` | `make demo-watsonx`; compose passes only the secret `WATSONX_API_KEY` as env — the non-secret `project_id`/`url` live in the mounted alias `provider_config`, **not** compose env ([RFC §E](0053-gemini-watsonx-providers.md#e-demo--compose-parity)). |
| [`internal/security/redactor.go`](../../internal/security/redactor.go) | IBM key-shape patterns added to the redactor allow-list. |
| [`docs/guides/model-providers.md`](../../docs/guides/model-providers.md) + `docs/manual-tests/` | Setup entry; `MT-PROVIDER-WATSONX-001` (single-provider smoke, live). |

#### Tests

- Tool round-trip maps correctly + reports `Usage`; the factory branch **fails closed** when `project_id`/`url` are absent; warns (not crashes) on a missing key.
- A society on `watsonx` completes a task + a persona turn against a recorded/mock transport; cost attributes to the priced alias.

#### PR checklist

- [ ] `pytest agents/tests/test_llm_watsonx.py -q`; `ruff`/`mypy` clean; `go test ./internal/security/...`.
- [ ] Factory fails closed on missing `project_id`/`url`; secret vs config split honoured (key in env, `project_id`/`url` in `provider_config`).
- [ ] Missing-price guard verified; `make demo-watsonx` boots; offline path maps to mock.

---

### PR 3: `feature/v0311-rfc0053-closeout` — Phase 3: Extras packaging + handoff + closeout

**Depends on**: PR 1 + PR 2 merged.
**Purpose**: Finalize the packaging + docs; hand the four-vendor enablement to [RFC 0052 PR 8](0052-pr-plan.md#pr-8-featurev0311-rfc0052-demo-multivendor--phase-4b-four-vendor-headline--closeout-cuttable). **No new provider code** — pinning four personas to four vendors in one channel is pure RFC 0033 alias config, and that blueprint + the cross-vendor MT live in RFC 0052.

#### Scope

| File | Change |
|------|--------|
| [`agents/pyproject.toml`](../../agents/pyproject.toml) | Confirm `persatrix[gemini,watsonx]` extras resolve; a combined extra if convenient. |
| [`docs/guides/model-providers.md`](../../docs/guides/model-providers.md) | The four-cloud-vendor roster section (Anthropic / OpenAI / Gemini / watsonx) + the secret-vs-config table. |
| RFC + ROADMAP + CHANGELOG | RFC 0053 front-matter → `✅ Implemented`; Master-Index row; CHANGELOG `[0.3.11]` provider entries; `make rfcs` regenerates [INDEX.md](INDEX.md). |

#### PR checklist

- [ ] Extras resolve; the model-providers guide documents all four cloud vendors.
- [ ] Handoff note points at RFC 0052 PR 8 for the cross-vendor blueprint + MT.
- [ ] RFC 0053 → ✅ Implemented; ROADMAP + CHANGELOG updated.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| A native SDK (google-genai / ibm-watsonx-ai) proves fiddly and drags the release. | Each provider is independent and cuttable; RFC 0052 ships on any single provider, so a dragging provider slips only the four-vendor headline, not the rung ([master plan §Risk](../v0.3.11-plan.md#risk-and-mitigations)). |
| An unpriced non-local alias silently disables the budget gate. | The missing-price guard (RFC 0033 §F, shipped) fails closed on any unpriced non-local alias; every demo alias ships explicitly priced. The guard checks *presence*, not accuracy — the RFC 0052 mandatory cap is the complementary second bound against price drift. |
| watsonx `project_id`/`url` mistaken for secrets and leaked into env/compose. | They are **config, not secrets** — the factory reads them from the alias `provider_config` and fails closed if absent; only `WATSONX_API_KEY` flows through env/compose ([RFC §C/§E](0053-gemini-watsonx-providers.md#c-watsonxai-provider)). |
| New optional deps burden single-provider deployments. | Shipped as **extras** (OQ #4); the `ImportError → SystemExit` hint means a non-user carries no new runtime dependency. |

---

## ROADMAP Hygiene

- **This planning PR** (the v0.3.11 plan) → RFC 0053 Master-Index row `📋 Proposed → 🚧 Implementing`, target `v0.3.x → v0.3.11`.
- **PR 1 / PR 2 merge** → CHANGELOG `[0.3.11]` provider entries seeded.
- **PR 3 (closeout) merges** → RFC 0053 → ✅ Implemented; `Last updated` refresh.

---

## Progress Overview

| PR | Phase | Branch | Status |
|----|-------|--------|--------|
| 1 | 1 — Gemini (native) | `feature/v0311-rfc0053-gemini` | ⬜ |
| 2 | 2 — watsonx.ai (native) | `feature/v0311-rfc0053-watsonx` | ⬜ |
| 3 | 3 — extras + handoff + closeout | `feature/v0311-rfc0053-closeout` | ⬜ |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged

---

## Related Documentation

- [RFC 0053 — Gemini and watsonx.ai LLM Providers](0053-gemini-watsonx-providers.md) — the spec.
- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — the [§H seam](0033-model-alias-layer.md#h-multi-provider-extensibility) + [§F pricing](0033-model-alias-layer.md#f-pricing-keyed-by-alias) this dogfoods.
- [RFC 0052 PR plan](0052-pr-plan.md) — the consuming four-vendor demo (PR 8) + the bundling.
- [v0.3.11-plan.md](../v0.3.11-plan.md) — the master version plan + locked decisions.
- [docs/guides/model-providers.md](../guides/model-providers.md) — the operator-facing provider setup guide this extends.
