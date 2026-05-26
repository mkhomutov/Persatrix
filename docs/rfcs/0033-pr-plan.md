# RFC 0033 — PR Implementation Plan (Provider-Agnostic Model Alias Layer — Phases 1–2)

**RFC**: [0033-model-alias-layer.md](0033-model-alias-layer.md)
**Created**: 2026-05-25
**Branch prefix**: `feature/v034-rfc0033-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.4-plan.md Phase 2 (RFC 0033 implementation)](../v0.3.4-plan.md#phase-2--implement-rfc-0033-phases-12)
**Amendment carried**: [v0.3.4-plan-amendment-2026-05-24.md](../v0.3.4-plan-amendment-2026-05-24.md) (provider-parity hardening — Phase 2 additions fold into the PRs below)

---

## Overview

RFC 0033 introduces a single source of truth for model identity: a named alias map (`quality` / `fast` / `summarizer`) that resolves a logical role into a concrete `(provider, model_id, pricing)` record. Agent configs reference aliases; a vendor retirement or a provider swap becomes a one-line edit to one map entry instead of a sweep across [`config/agents.yaml`](../../config/agents.yaml), [`config/optimization.yaml`](../../config/optimization.yaml), [`agents/persona_types.py`](../../agents/persona_types.py), and the pricing table. The proximate trigger is the Anthropic [Sonnet 4 retirement](https://platform.claude.com/docs/en/about-claude/model-deprecations) (2026-06-15); the alias layer absorbs that migration as the first exercise of the abstraction — one `quality` alias edit, not a per-agent sweep ([RFC §Motivation](0033-model-alias-layer.md#motivation)).

This plan covers **Phases 1–2** of the [RFC §Phased Implementation Plan](0033-model-alias-layer.md#phased-implementation-plan) — the v0.3.4 contract per the [master plan](../v0.3.4-plan.md#phase-2--implement-rfc-0033-phases-12). Phase 3 (raw-ID pass-through removal + `_infer_provider` retirement) is scoped under [§Future Phases](#future-phases) with no PR rows: it opens only when dogfood shows zero raw-ID startup warnings (observed traffic, not a calendar date — per the project preference against pre-production calendar gates).

The work splits into **7 PRs**: Phase 1 is three implementation PRs (resolver substrate → factory integration → config migration), Phase 2 is three more (missing-price guard → telemetry + alias-derived pricing → documentation sweep), and one Phases-1–2 closeout PR. The closeout is a **partial-RFC closeout** mirroring the [RFC 0024 PR plan](0024-pr-plan.md) precedent, because the full-RFC closeout waits for Phase 3 in v0.3.5+. Review-follow-up PRs, if review surfaces findings, slot in before the closeout following the [RFC 0024 PR 5 / PR 5.1](0024-pr-plan.md) precedent rather than being pre-numbered here. Each PR leaves the repo in a passing-tests, lint-clean state and stays within the [BRANCHING.md](../BRANCHING.md) review surface.

**The release's name is *Any Model, Any Provider*** — so this plan carries the [provider-parity amendment](../v0.3.4-plan-amendment-2026-05-24.md)'s Phase 2 additions, not only the RFC's original Phase 2:

- **Missing-price guard** (amendment item 1) — *the one genuine safety item*. `EstimateCost` returns `$0` for any model absent from the pricing table ([`internal/cost/config.go:138`](../../internal/cost/config.go)), which silently disables the RFC 0023 pre-call lease gate. "Any Model" makes that reachable (an operator-added alias or a real Ollama tag with no price). PR 4 closes it for the alias surface.
- **OpenAI as a first-class peer** (amendment item 2) — the alias block carries an OpenAI entry and OpenAI pricing rows, so an "Any Provider" release ships a priced cloud alternative, and the master-plan Phase 4 swap test has a target.
- **Network-allowlist neutralization** (amendment item 5) — the Anthropic-only `network.allow` in [`config/agents.yaml`](../../config/agents.yaml) is generalized in the migration sweep so example configs no longer read as single-vendor.
- **Local-model pricing decision** (amendment item 6) — offline / Ollama aliases either carry an explicit *simulation* price (so the wallet-cap demo still trips) or are documented $0-real with the cap demo pinned to a priced alias.

The first-run onboarding pass (amendment item 4: `.env`, compose guard, `make demo-openai`) and the one-line-swap manual test (amendment item 3) are **master-plan Phase 4** (release-prep), not RFC 0033 implementation — out of scope here, but this plan's OpenAI alias + pricing (PRs 1 / 5) is what gives the Phase 4 swap test a priced target.

**Prerequisites**:
- [RFC 0004](0004-python-agent-grpc-server.md) (Python Agent gRPC Server) — shipped; established the `LLMProvider` Protocol the alias layer sits *above*. Untouched: the resolver lives in the factory and config layer, not in the provider surface.
- [RFC 0008](0008-agent-memory-context-optimization.md) (Memory & Context Optimization) — shipped; established [`config/optimization.yaml`](../../config/optimization.yaml) as the routing/cost config home where the `models.aliases` block lands.
- [RFC 0023](0023-llm-call-leasing.md) (LLM Call Leasing) — shipped in v0.3.2; the pre-call lease gate the missing-price guard (PR 4) protects. The cost-attribution gate (PR 5) defends its `GET /api/v1/cost/summary` surface across the alias re-keying.
- [RFC 0019](0019-opentelemetry-completion.md) (OpenTelemetry Completion) — shipped; the `gen_ai.*` span conventions and the `persatrix.*` reserved-namespace rule the new `persatrix.llm.model_alias` attribute (PR 5) is additive within.
- Offline `MockProvider` ([#422](https://github.com/mkhomutov/Persatrix/pull/422)) and Ollama ([#423](https://github.com/mkhomutov/Persatrix/pull/423)) — already merged to `main`; PR 2's factory rewrite must keep both working (the interplay regression).

**Hard gates**:
- **Cost attribution must not regress across the re-keying.** [Master-plan §Acceptance](../v0.3.4-plan.md#acceptance-for-v034) makes alias-keyed, non-zero cost reporting a plan-level gate. PR 5 derives the legacy pricing block from the alias map ([RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias)), emits `persatrix.llm.model_alias` ([RFC §G](0033-model-alias-layer.md#g-telemetry)), and asserts an alias-routed agent reports correctly-keyed, non-zero cost via `GET /api/v1/cost/summary`.
- **The missing-price path fails closed for non-local providers.** [Amendment §Acceptance additions](../v0.3.4-plan-amendment-2026-05-24.md#acceptance-additions): a resolved alias with no pricing entry produces a loud startup warning and fails closed for non-local providers — never a silent `$0` estimate. The guard is scoped to the alias surface, leaving the deliberately-preserved raw-ID fall-through ([§E](0033-model-alias-layer.md#e-backwards-compatibility)) untouched. PR 4 closes it; unit coverage proves a local ($0-by-design) alias is distinguishable from an unpriced one.
- **Offline / Ollama keep working after the resolver lands.** `provider: mock` / `provider: ollama` resolve through the same factory path; PR 2 adds a regression check that `make demo-offline` ([#422](https://github.com/mkhomutov/Persatrix/pull/422)) and `make demo-ollama` ([#423](https://github.com/mkhomutov/Persatrix/pull/423)) survive the `create_provider` rewrite.

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 7**. PR 1 lands the resolver substrate with no consumer (raw-ID path unchanged). PR 2 wires the factory to consume it (configs still carry raw IDs, so behaviour is unchanged but the startup warning now fires). PR 3 migrates the configs to aliases — after PR 3, no runtime path carries a literal vendor model ID. PR 4 (missing-price guard) depends only on PR 1's alias map and can land in parallel with PR 2/PR 3. PR 5 (telemetry + pricing) needs PR 2 (resolution wired) and PR 3 (configs on aliases) so the cost gate exercises an alias-routed agent. PR 6 (doc sweep) needs PR 3 (aliases live). PR 7 closes out.

---

## Dependency Graph

```
[Hard gates]
  Cost attribution must not regress across the alias re-keying      → PR 5 (alias-derived pricing + model_alias span + /cost/summary gate)
  Missing-price fails closed for non-local providers                → PR 4 (guard scoped to alias surface; raw-ID fall-through untouched)
  Offline / Ollama keep working after the resolver lands            → PR 2 (provider: mock / ollama through the same path + regression check)
  ↓
PR 1 (agents/model_aliases.py resolver + models.aliases block + loader; OpenAI alias; local-pricing decision)   [RFC Phase 1]
  ├─────────────────────────────────────────────┐
  ↓                                              ↓
PR 2 (create_provider → (provider, model) tuple; §D precedence;   PR 4 (missing-price guard: warn + fail-closed for
      offline/Ollama interplay regression; raw-ID startup warning        unpriced non-local aliases; local $0 distinguishable)
      + persatrix.llm.alias.raw_id_usage counter)              [RFC Phase 1]   [RFC Phase 2 + amendment item 1]
  ↓
PR 3 (agents.yaml + summarization.model → aliases; Sonnet 4→4.6 via `quality`;
      network-allowlist neutralized; SubAgentRequest.model None-default + §J resolution)   [RFC Phase 1 + amendment item 5]
  ↓
PR 5 (persatrix.llm.model_alias span attr + alias-derived cost.pricing; /cost/summary cost gate; OpenAI rows)   [RFC Phase 2 + amendment item 2]
  ↓
PR 6 (documentation sweep — extension-spec, persona-agents guide, orchestration-spec, RFC examples)   [RFC Phase 2]
  ↓
PR 7 (Phases-1–2 closeout — status: ⚠️ Partially Implemented (Phases 1–2))
```

PR 1 is additive and unconsumed: the resolver module and `models.aliases` block land, but `create_provider` does not yet call `resolve()`, so behaviour is unchanged. PR 2 is the only PR that changes the factory's return contract (a tuple), but configs still carry raw IDs that take the pass-through, so observable routing is unchanged — what changes is that a startup deprecation warning now fires per raw-ID agent. PR 3 flips the configs to aliases; the Sonnet 4 → 4.6 migration is absorbed by editing only the `quality` alias entry's `model:` field. PRs 4–6 are Phase 2: a safety guard, the telemetry/pricing re-keying, and a doc sweep. PR 7 is doc-only.

---

## PR Sequence

### PR 1: `feature/v034-rfc0033-resolver` — Resolver Module + `models.aliases` Config Block

**Depends on**: nothing (builds on the v0.3.4 baseline; offline/Ollama already on `main`).
**Purpose**: Land the alias substrate. Introduce a new leaf module `agents/model_aliases.py` with the `ResolvedModel` dataclass and `resolve()` function ([RFC §C](0033-model-alias-layer.md#c-resolver)), add the `models.aliases` block to [`config/optimization.yaml`](../../config/optimization.yaml) ([RFC §B](0033-model-alias-layer.md#b-config-shape)), bump `schema_version` to `"0.2"`, and load/expose the block via [`agents/optimization.py`](../../agents/optimization.py). **No consumer yet** — `create_provider` is not rewired until PR 2, so this PR changes no runtime behaviour. The alias block ships an OpenAI entry ([amendment item 2](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)) and records the local-model pricing decision ([amendment item 6](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)) so later PRs build on a complete map.

#### Scope

| File | Change |
|------|--------|
| `agents/model_aliases.py` | **New** — leaf module, no agent-runtime imports. `ResolvedModel` frozen dataclass (`alias: str \| None`, `provider`, `model`, `input_per_1m_tokens`, `output_per_1m_tokens`, `provider_config: dict`, `raw: bool`) per [RFC §C](0033-model-alias-layer.md#c-resolver). `resolve(alias_or_model: str) -> ResolvedModel`: alias hit returns the configured record; no-alias-match falls through to the existing prefix table ([`agents/optimization.py`](../../agents/optimization.py) `provider_inference()`) and returns `alias=None, raw=True`. Reads from a process-wide singleton populated at module load; loading is lazy + cached with a context-manager test seam so tests register a temporary map without disturbing the singleton. |
| [`config/optimization.yaml`](../../config/optimization.yaml) | Add the top-level `models.aliases` block: `quality` → `{anthropic, claude-sonnet-4-6, 3.00, 15.00}`, `fast` / `summarizer` → `{anthropic, claude-haiku-4-5-20251001, 0.80, 4.00}`, **plus at least one OpenAI alias** (e.g. `quality-openai` → `{openai, <model>, <input>, <output>}`) so the release ships a priced cloud peer ([amendment item 2](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)). Bump `schema_version: "0.1"` → `"0.2"` (line 2). **No `default.model_routing.defaults` / `agents.yaml` / pricing rewrite in this PR** — that is PR 3 / PR 5; PR 1 only adds the block. |
| [`agents/optimization.py`](../../agents/optimization.py) | Load + expose the `models.aliases` block (same accessor shape as the existing `provider_inference()` / `active_profile` accessors). The `model_aliases` singleton reads through this. |
| [`schemas/optimization.schema.json`](../../schemas/optimization.schema.json) | Add the `models.aliases` schema: each alias entry requires `provider` + `model` + `input_per_1m_tokens` + `output_per_1m_tokens`, with optional `provider_config` (passthrough — e.g. `base_url`) and `notes`. Accept `schema_version: "0.2"`. |
| `tests/unit/python/test_model_aliases.py` | **New** — alias hit returns the configured `ResolvedModel`; raw-ID fall-through returns `ResolvedModel(alias=None, raw=True)`; unknown alias **and** unknown raw prefix → `SystemExit` naming the offending string ([RFC §Test Strategy](0033-model-alias-layer.md#test-strategy)); the test seam registers a temporary map without mutating the singleton. |
| [`tests/unit/python/test_optimization.py`](../../tests/unit/python/test_optimization.py) | Extend — `models.aliases` block parses; `schema_version: "0.2"` accepted by the loader path. |

#### Key implementation details

- **`agents/model_aliases.py` is a leaf.** [RFC §C](0033-model-alias-layer.md#c-resolver) places it below the agent runtime — it imports config accessors only, never the provider classes or `LLMClient`. This keeps the resolver unit-testable in isolation and avoids an import cycle with `agents/llm_client.py` (which will import *it* in PR 2).
- **No consumer in this PR by design.** The resolver and block land unconsumed so the substrate PR is reviewable on its own and the factory rewrite (PR 2) is a focused diff. Until PR 2, every model reference still flows through the unchanged `create_provider` raw-string path — this PR cannot regress routing.
- **Local-model pricing decision recorded here** ([amendment item 6](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)). The RFC's own `local-fast` example prices a local model at `0`, which would mean the simulated wallet never trips — contradicting the README's "an agent pauses itself at the cap." This PR's commit message + a `notes:` field on the offline/Ollama-facing alias entries record the decision: either carry an explicit **simulation** price so the wallet-cap demo still trips, or document the alias as $0-real and pin the cap demo to a priced alias. The decision is data on the alias entry, not new code.
- **OpenAI alias is priced, not just declared** ([amendment item 2](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)). Because PR 5 derives the pricing table from the alias map, an OpenAI alias with no `input_per_1m_tokens` / `output_per_1m_tokens` would ship no OpenAI price. The entry carries real rows so the master-plan Phase 4 one-line-swap test has a priced target.

#### Tests

- Alias hit → configured `ResolvedModel`; raw-ID fall-through → `alias=None, raw=True`; unknown string → `SystemExit`.
- `models.aliases` block parses; `schema_version: "0.2"` accepted; OpenAI alias entry round-trips with its pricing fields.
- Test seam: a temporarily-registered map is visible to `resolve()` and torn down without mutating the process singleton.

#### PR checklist

- [ ] `pytest tests/unit/python/test_model_aliases.py tests/unit/python/test_optimization.py -q` passes.
- [ ] `cd agents && mypy .` clean (whole-package, as CI runs it); `ruff check agents/` clean.
- [ ] `make validate` passes against the new `models.aliases` schema and `schema_version: "0.2"`.
- [ ] `make test` clean — no runtime behaviour change (resolver is unconsumed; raw-ID path unchanged).
- [ ] `models.aliases` block carries a priced OpenAI alias ([amendment item 2](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)); local-pricing decision recorded in the PR description + alias `notes:` ([amendment item 6](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)).
- [ ] No RFC 0033 status change (substrate; first implementation PR is PR 2 — see [ROADMAP Hygiene](#roadmap-hygiene)).
- [x] [Progress Overview](#progress-overview) row 1 filled.

---

### PR 2: `feature/v034-rfc0033-factory` — Factory Integration + Raw-ID Startup Warning

**Depends on**: PR 1 merged (`resolve()` + alias map available).
**Purpose**: Rewire [`agents/llm_client.py`](../../agents/llm_client.py) `create_provider` to return `(provider, physical_model)` and consume `resolve()` ([RFC §D](0033-model-alias-layer.md#d-factory-integration)), update the call sites to use the returned physical model in `create_message(model=…)`, encode the [§D precedence rules](0033-model-alias-layer.md#d-factory-integration), and emit the raw-ID startup deprecation warning + the Phase-3 gate counter ([RFC Phase 1 deliverable #9](0033-model-alias-layer.md#phase-1--resolver--alias-config-block--first-migration) + the [Phase 3 gate metric source](0033-model-alias-layer.md#phased-implementation-plan)). Configs still carry raw vendor IDs at this point (the pass-through handles them), so **observable routing is unchanged** — what changes is that the warning fires per raw-ID agent. The offline / Ollama force-flags and per-agent `provider: mock` / `provider: ollama` paths keep working through the same factory.

#### Scope

| File | Change |
|------|--------|
| [`agents/llm_client.py`](../../agents/llm_client.py) | `create_provider(agent_config) -> tuple[LLMProvider, str]` ([RFC §D](0033-model-alias-layer.md#d-factory-integration)). After the offline/Ollama force-flag short-circuits (lines 406–437, kept verbatim), call `resolve(agent_config["model"])` and branch on `resolved.provider`; return `(provider_instance, resolved.model)`. Encode [§D precedence rule 1](0033-model-alias-layer.md#d-factory-integration): when `model:` resolves to an alias, the alias's `provider` is authoritative and an explicit disagreeing `provider:` field on the agent entry is a `SystemExit` naming the agent id, the alias, and both providers; today's `agent_config.get("provider") or _infer_provider(model)` precedence (line 444) is preserved **only** on the raw-ID pass-through. Encode [§D precedence rule 2](0033-model-alias-layer.md#d-factory-integration): alias-level `provider_config` wins for fields it declares, agent-entry `provider_config` fills only the gaps. `_infer_provider` (lines 371–383) is **kept** — it is the raw-ID fall-through engine through Phase 2; its retirement is Phase 3. |
| [`agents/server_persona.py`](../../agents/server_persona.py) | Update the `create_provider` call site (line 212) to unpack the tuple: `provider, physical_model = create_provider(agent_config)`; thread `physical_model` to the `LLMClient` / `create_message` path so the API call goes to the vendor ID, not the alias name. |
| [`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py) (summarization read-site) | `create_provider` has **one** caller today — `server_persona.py:212` (row above), immediately wrapped by `LLMClient(provider)` (line 213). Sub-agents (`spawn_sub_agent` → orchestrator) and summarization **thread that single `LLMClient`** rather than each calling `create_provider`, so the factory has no other tuple-return call sites to update — the [RFC §D](0033-model-alias-layer.md#d-factory-integration) "~3 caller sites" line predates this consolidation. Summarization picks its model on a **separate** surface that never touches `create_provider`: [`summarize_close.py:148`](../../agents/persona_runtime/summarize_close.py) reads [`summarization_model()`](../../agents/optimization.py) and passes the value straight into `create_message(model=…)`. This PR wires that read-site through `resolve()` as well — while the field is still the raw Haiku ID (PR 3 migrates it), `resolve()` returns it unchanged, so routing and the raw-ID warning match the factory path. PR 3's flip to the `summarizer` alias then resolves *here*, the same way the `agents.yaml` flip resolves in the factory, so the literal `"summarizer"` never reaches the vendor API. |
| [`agents/llm_client.py`](../../agents/llm_client.py) (warning + counter) | At config load / provider creation, if `resolved.raw` is true (a raw vendor model ID), emit a one-shot-per-process startup deprecation warning naming the agent and the raw model, and increment a `persatrix.llm.alias.raw_id_usage{agent_id}` OTEL counter once per agent ([RFC Phase 3 gate metric source](0033-model-alias-layer.md#phased-implementation-plan)). The counter rides the existing `persatrix.llm.*` export path (no new pipeline). This counter reading zero across dogfood is the Phase 3 entrance signal. |
| [`tests/unit/python/test_llm_client.py`](../../tests/unit/python/test_llm_client.py) | Update for the tuple return; assert the physical-model passthrough (alias `quality` → `create_message(model="claude-sonnet-4-6")`); §D precedence — disagreeing alias/`provider:` raises `SystemExit`; raw-ID path emits the deprecation warning exactly once and increments `raw_id_usage`. |
| `tests/unit/python/test_llm_client_offline_ollama.py` (or the existing offline/Ollama test module) | **Regression check** — `provider: mock` / `PERSATRIX_OFFLINE=1` and `provider: ollama` / `PERSATRIX_OLLAMA=1` still select `MockProvider` / `OllamaProvider` after the rewrite; the force-flag short-circuit precedence (offline wins over Ollama) is unchanged; an alias whose entry declares `provider: ollama` resolves through the same branch. Keeps [#422](https://github.com/mkhomutov/Persatrix/pull/422) / [#423](https://github.com/mkhomutov/Persatrix/pull/423) green. |

#### Key implementation details

- **The force-flag short-circuits stay first.** [`create_provider`](../../agents/llm_factory.py) checks `offline_mode_enabled()` / `provider == "mock"` and `ollama_mode_enabled()` **before** the model field, so a keyless demo config works. PR 2 keeps that ordering — `resolve()` runs only on the non-forced path. This is why the offline/Ollama interplay regression is a hard gate, not an afterthought: a careless rewrite that moved `resolve()` ahead of the force-flag check would break `make demo-offline` (which carries a placeholder model id with no key).
- **The tuple return is the load-bearing change.** Today `create_provider` returns just `LLMProvider` and the caller reads `agent_config["model"]` for `create_message`. Under aliasing the alias name (`quality`) must never reach `create_message` — the vendor ID (`claude-sonnet-4-6`) must. Returning `(provider, resolved.model)` makes the physical model the single value the caller threads to the API call. Every call site is updated in this PR; `test_llm_client.py` pins that the alias name never appears in a `create_message(model=…)` argument.
- **Behaviour is unchanged because configs are still raw here.** PR 2 ships the machinery but does not migrate configs (PR 3 does). Every stock agent still says `model: "claude-sonnet-4-20250514"`, which `resolve()` returns as `raw=True` and routes exactly as before — *plus* the new deprecation warning. This staging keeps PR 2's diff to the factory and PR 3's diff to the configs.
- **`_infer_provider` is preserved, not retired.** [RFC §I](0033-model-alias-layer.md#i-retirement-of-_infer_provider) retires it only in Phase 3, gated on zero raw-ID usage. PR 2 keeps it (now the resolver's own `_infer_raw_provider` is the live raw-ID engine via `resolve()`); `_infer_provider` stays pinned by the resolver-mirror test and is removed in Phase 3 alongside the pass-through. The `raw_id_usage` counter this PR adds is the signal that authorises that retirement.
- **Factory extracted to `agents/llm_factory.py`.** [`agents/llm_client.py`](../../agents/llm_client.py) was already at the 500-line repo cap, so the rewrite would have pushed it over. `create_provider` (+ the raw-ID signal helpers) moved to a new leaf module [`agents/llm_factory.py`](../../agents/llm_factory.py) and is **re-exported** from `llm_client.py`, so the `from agents.llm_client import create_provider` path is unchanged. The dedup state + `try_get_instruments` hook live in `llm_factory`; the factory's unit tests are in [`tests/unit/python/test_llm_factory.py`](../../tests/unit/python/test_llm_factory.py). No import-path break, no cycle (`llm_factory` imports the provider/leaf modules + the resolver, never `llm_client`).
- **Summarisation surface resolves too, and degrades on failure.** The summariser model ([`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)) is a *second* model surface that `create_provider` does not touch, so it calls `resolve()` itself. Unlike the factory's startup resolution, this runs per-close on a **background** task whose caller guards only `except Exception` — so an unresolvable summarisation model degrades to the deterministic fallback summary + the `agent.interactions.summary.failed{reason=model_unresolvable}` metric, rather than escaping as an uncaught `SystemExit`. (Today the field is a recognised raw Haiku id, so this never fires; it is the regression guard for PR 3's flip to the `summarizer` alias and any future local-only summariser.)
- **The Phase 3 gate counter has a known scope.** `persatrix.llm.alias.raw_id_usage` counts only the `create_provider` (agent) surface; the summarisation surface above is **not** counted. PR 3 must migrate *both* the agent configs **and** the `context_management.summarization.model` field to aliases — only then is a zero counter reading an authoritative "no raw IDs left" Phase 3 signal.

#### Tests

- Tuple shape + physical-model passthrough: alias `quality` resolves and `create_message` receives `claude-sonnet-4-6`, never `"quality"`.
- Physical-model threading at `load_agent`: the resolved physical model lands in `agent.config["model"]` (what `BaseAgent` sends), never the alias name; raw path is a no-op ([`test_server_load_agent.py`](../../tests/unit/python/test_server_load_agent.py) `TestPhysicalModelThreading`).
- §D precedence: an agent entry whose `model:` is an alias and whose `provider:` disagrees → `SystemExit` naming both; alias `provider_config` wins per-field over the agent entry's.
- Raw-ID path: deprecation warning once per process; `persatrix.llm.alias.raw_id_usage` increments once per raw-ID agent.
- Summariser surface: an unresolvable summarisation model degrades to the fallback + `summary.failed` counter, not `SystemExit` ([`test_summarize_close_helpers.py`](../../tests/unit/python/test_summarize_close_helpers.py) `TestUnresolvableSummarizationModelFallsBack`).
- Offline/Ollama regression: force-flags and `provider: mock` / `provider: ollama` still select the right provider; offline-wins-over-Ollama precedence intact.

#### PR checklist

- [x] `pytest tests/unit/python/test_llm_factory.py tests/unit/python/test_llm_client.py tests/unit/python/test_model_aliases.py -q` plus the offline/Ollama regression modules (`test_llm_offline.py` / `test_llm_ollama.py`) pass.
- [x] `cd agents && mypy .` clean (whole-package); `ruff check agents/` clean.
- [x] `make test` clean — observable routing unchanged (configs still raw; pass-through active). Verified via the touched unit modules + `agents/tests` (387 passed) + mypy/ruff; no Go change; CI runs the full target.
- [x] `create_provider` returns `(provider, physical_model)`; no call site passes an alias name into `create_message(model=…)` (`server_persona.py` threads the physical model; `summarize_close.py` resolves its separate model surface).
- [x] §D precedence rules encoded and tested (disagreeing alias/`provider:` → `SystemExit`; alias `provider_config` per-field precedence).
- [x] Raw-ID startup deprecation warning fires once per process; `persatrix.llm.alias.raw_id_usage{agent_id}` counter increments once per raw-ID agent (the Phase 3 gate signal).
- [x] **Offline / Ollama interplay regression green** — `provider: mock` / `provider: ollama` (incl. an alias declaring `provider: ollama`) resolve through the rewritten factory; offline-wins-over-Ollama precedence intact ([#422](https://github.com/mkhomutov/Persatrix/pull/422) / [#423](https://github.com/mkhomutov/Persatrix/pull/423)). End-to-end `make demo-offline` / `make demo-ollama` re-run is master-plan Phase 4.
- [x] [RFC 0033 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening (first implementation PR); [v0.3.4-plan Master Progress Overview](../v0.3.4-plan.md#master-progress-overview) row 2 → 🔄 In progress.
- [x] [§Version Map](../../ROADMAP.md#version-map) v0.3.4 row stays `🚧 Planning` (no version-map flip mid-implementation).
- [x] [Progress Overview](#progress-overview) row 2 filled.

---

### PR 3: `feature/v034-rfc0033-migration` — Config Migration + Drop the Last Code Literal

**Depends on**: PR 2 merged (factory consumes `resolve()`; alias resolution wired).
**Purpose**: Migrate every config-side and code-side literal vendor model ID to an alias. After this PR, **no runtime path carries a literal vendor model ID** ([master-plan §Acceptance](../v0.3.4-plan.md#acceptance-for-v034)) — every reference flows through an alias (resolved at call time) or the raw-ID pass-through (config only, removed in Phase 3). The Anthropic Sonnet 4 → 4.6 migration is **absorbed by editing only the `quality` alias entry's `model:` field** (done in PR 1's block; this PR points the agents at `quality`), with no per-agent sweep. Generalize the Anthropic-only example network allowlist ([amendment item 5](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)).

#### Scope

| File | Change |
|------|--------|
| [`config/agents.yaml`](../../config/agents.yaml) | Migrate all six `model:` fields (lines 11, 30, 72, 98, 215, 280 — covers both `type: task` and `type: persona` per [RFC §J](0033-model-alias-layer.md#j-persona-and-sub-agent-model-selection)) from `"claude-sonnet-4-20250514"` to the `quality` alias. One pass, both kinds. The startup deprecation warning (PR 2) stops firing for these agents. |
| [`config/agents.yaml`](../../config/agents.yaml) (network allowlist) | Generalize (or drop) the Anthropic-only `network.allow: ["api.anthropic.com"]` blocks (lines 22–24, 64–66, 87–89) so example configs no longer read as single-vendor ([amendment item 5](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)). Unenforced today, but it contradicts the *Any Provider* theme; replace with a provider-neutral example or remove the example allowlist. |
| [`config/optimization.yaml`](../../config/optimization.yaml) | Rewrite `default.model_routing.defaults` (lines 10–12) to alias names (`task_agents: quality`, `sub_agents: quality`, `evaluators: fast`). Rewrite `context_management.summarization.model` (line 36) from `"claude-haiku-4-5-20251001"` to the `summarizer` alias ([RFC §B](0033-model-alias-layer.md#b-config-shape) — the third literal surface; leaving it raw would mean a Haiku swap still needs a hand-edit, defeating the one-alias-edit property). This field is read on the **separate** summarization surface ([`summarize_close.py:148`](../../agents/persona_runtime/summarize_close.py)), not through `create_provider`; it resolves via the `resolve()` wiring PR 2 added at that read-site, so the alias flip works the same way the factory flip does. |
| [`agents/persona_types.py`](../../agents/persona_types.py) | Drop the hardcoded vendor ID from `SubAgentRequest.model` (line 118): `model: str \| None = None` ([RFC §J.3](0033-model-alias-layer.md#j-persona-and-sub-agent-model-selection)). This is the *only* code-level model literal in the runtime. |
| Sub-agent factory (`agents/sub_agents/`) | Resolve `SubAgentRequest.model is None` at construction time to the active profile's `default.model_routing.defaults.sub_agents` alias ([RFC §J.3](0033-model-alias-layer.md#j-persona-and-sub-agent-model-selection)); callers may still pass an explicit alias (`SubAgentRequest(..., model="fast")`). |
| `tests/unit/python/test_model_aliases.py` / sub-agent factory tests | Add the `None`-default resolution path ([RFC Phase 1 deliverable #8](0033-model-alias-layer.md#phase-1--resolver--alias-config-block--first-migration)): `SubAgentRequest()` with no `model` resolves to the `sub_agents` default alias; an explicit `model="fast"` is honoured. |
| `tests/unit/python/test_llm_client.py` | Assert a clean checkout starts default agents on `claude-sonnet-4-6` (the migration landed) and emits **no** raw-ID deprecation warning for stock configs. |

#### Key implementation details

- **The Sonnet 4 → 4.6 migration is one alias edit, demonstrated here.** The `quality` alias already points at `claude-sonnet-4-6` (PR 1). This PR's only "migration" action for the deadline is pointing the six agents and the routing defaults at `quality` — the physical model swap was a single line in PR 1's block. This is the RFC's headline property made concrete: the next retirement is also one line.
- **After this PR, no runtime code carries a literal vendor model ID** ([RFC §J](0033-model-alias-layer.md#j-persona-and-sub-agent-model-selection)). `SubAgentRequest.model` was the last one; dropping it to `None` + resolving at construction closes the gap. Vendor IDs remain only in config (one per alias entry), which is the intended single source of truth.
- **Network-allowlist neutralization is a theme-consistency fix, not a behaviour change.** The `network.allow` blocks are unenforced today; the change is purely so the shipped example configs do not advertise a single-vendor assumption that the release's name contradicts ([amendment item 5](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)).
- **Summarization path closes the third literal surface.** [RFC §B](0033-model-alias-layer.md#b-config-shape) calls out `context_management.summarization.model` as a separate literal from the routing defaults and the pricing keys. Migrating it to `summarizer` is what makes a future Haiku retirement also a one-alias edit. It is **also a Phase 3 gate requirement**: PR 2's `persatrix.llm.alias.raw_id_usage` counter does **not** observe this surface (it covers only `create_provider`), so a raw summariser model is invisible to the gate. Until this field is an alias, a zero counter reading is *not* a complete "no raw IDs left" signal.

#### Tests

- Clean checkout: default agents resolve to `claude-sonnet-4-6` via `quality`; no raw-ID warning for stock configs.
- Sub-agent `None`-default resolves to the `sub_agents` alias; explicit `model="fast"` honoured.
- `make validate` accepts the alias-referencing `agents.yaml` and the neutralized network blocks.

#### PR checklist

- [x] `pytest tests/unit/python/test_optimization_routing.py tests/unit/python/test_sub_agent_model_default.py tests/unit/python/test_llm_factory.py tests/unit/python/test_model_aliases.py tests/unit/python/test_optimization.py -q` pass; full unit suite green (the §J.3 sub-agent-default tests live in `test_sub_agent_model_default.py`, the routing-accessor tests in `test_optimization_routing.py` — split out of `test_optimization.py` to stay under the file-size cap).
- [x] `cd agents && mypy .` clean (whole-package, as CI runs it); `ruff check` clean (agents + tests).
- [x] `make validate` passes against the alias-referencing `agents.yaml` + neutralized network blocks; `make test` clean (Python; Go untouched — no Go test reads the shipped config).
- [x] All six `agents.yaml` `model:` fields + the three `default.model_routing.defaults` entries + `summarization.model` reference aliases; **Sonnet 4 → 4.6 done by editing only the `quality` alias** — no per-agent sweep.
- [x] `SubAgentRequest.model` default is `None`; `__post_init__` resolves it to the `sub_agents` alias — **no runtime path carries a literal vendor model ID**.
- [x] Anthropic-only example `network.allow` blocks generalized to a provider-neutral allowlist ([amendment item 5](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)).
- [x] [Progress Overview](#progress-overview) row 3 filled.

#### Review findings (PR #433)

| Finding | Severity | Disposition |
|---------|----------|-------------|
| `SubAgentRequest.__post_init__` comment claimed `sub_agent_default_model` has a `quality` fallback in a config-less checkout — it has none (it `SystemExit`s), contradicting the implementation, the accessor docstring, and the no-hardcoded-defaults principle. | Low (doc) | **Fixed in-PR** — comment corrected to describe the loud-fail behaviour; existing `test_sub_agent_model_default.py::test_none_model_raises_loud_when_routing_default_absent` already pins it. |
| `default.model_routing.defaults.task_agents` / `.evaluators` are migrated to aliases and surfaced by `model_routing_defaults()`, but no runtime path consumes them — only `sub_agents` is wired (via `sub_agent_default_model`). An agent with no usable `model:` is hard-stopped (schema-rejected if absent, `SystemExit` if empty), never falling back to the routing default. Pre-dates this PR; the new accessor only makes it more visible. | Low | **Deferred** → [ISSUE-0069](../issues/ISSUE-0069-task-agent-evaluator-routing-defaults-unconsumed.md) (wire the two roles, or document the keys as reserved). |
| **Cost regression — USD spend caps read $0 for the alias-routed workload.** `cost.pricing.models` still keys the retired `claude-sonnet-4-20250514`; flipping the agents to `quality` (→ physical `claude-sonnet-4-6`) leaves that physical ID unpriced, so `EstimateCost` returns $0 ([internal/cost/config.go:142-149](../../internal/cost/config.go)) for every `quality`-routed call (task agents + personas + sub-agents). The pre-call lease, recorded usage, and pre-dispatch budget check all read that $0, so the USD budgets never accrue — a regression vs. the pre-PR raw ID, which was priced. Evaluators (`fast` → still-priced Haiku) are unaffected. | Low (pre-production: no live spend; the cost-attribution gate lands before release) | **Deferred** → PR 5 (derives `cost.pricing.models` from the alias map, [RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias)). **NB PR 4 does _not_ close this**: its guard validates *alias-entry inline* pricing and `quality` carries it (3.00/15.00), so the guard passes; only PR 5's re-keying of the legacy table prices `claude-sonnet-4-6` on the Go side. No manual pricing row added here — it would perpetuate the alias-vs-legacy duplication PR 5 removes, and PR 5 regenerates the block wholesale. |
| `SubAgentRequest.__post_init__` resolves the model default via `sub_agent_default_model()`, which raises `SystemExit` (a `BaseException`, not caught by `except Exception`) when config lacks the `sub_agents` routing default. Latent (`SPAWN_SUB_AGENT` is a stub); when wired, the spawn site must catch it as `summarize_close.py` does, or a misconfig tears down the loop. Also couples a previously-pure data type to the global config cache. | Low (latent) | **Deferred** → [ISSUE-0070](../issues/ISSUE-0070-sub-agent-request-post-init-systemexit-construction.md). |
| `config/agents.yaml` network-allowlist comment said the list is "unenforced today" — but `PermissionGate.is_domain_allowed` does gate the `http_request` builtin tool ([agents/tools/builtin.py:282](../../agents/tools/builtin.py)). Harmless for these agents (none carry `http_request`), but the comment also conflated the list with LLM-provider egress, which it never gates. | Low (doc) | **Fixed in-PR** — comment corrected: the list is the `http_request` domain allowlist, inert for these toolless agents, not LLM-provider egress. |
| [ISSUE-0069](../issues/ISSUE-0069-task-agent-evaluator-routing-defaults-unconsumed.md) impact text said omitting `model:` "gets `SystemExit`" — but `model` is schema-`required` ([schemas/agent.schema.json](../../schemas/agent.schema.json)), so an omitted field is rejected by `make validate`; an absent key would `KeyError` at [llm_factory.py:170](../../agents/llm_factory.py), and `SystemExit` is specifically the empty-`model: ""` case. | Low (doc) | **Fixed in-PR** — ISSUE-0069 summary/Context/Impact tightened. |
| `templates/sub_agents.yaml` still carries the literal `claude-sonnet-4-20250514` (×4). Dormant — template composition is inactive ([RFC 0005](0005-persona-agent-memory.md)), so no runtime path reads it; out of this PR's `config/` scope, and the master-plan acceptance ("no runtime path carries a literal vendor model ID") still holds. | Info | **No action this PR** — migrate the literal if/when template composition is activated. |

---

### PR 4: `feature/v034-rfc0033-missing-price-guard` — Missing-Price Guard (Fail-Closed for Unpriced Non-Local Aliases)

**Depends on**: PR 1 merged (alias map + provider locality available). Independent of PR 2/PR 3 — can land in parallel.
**Purpose**: Close *the one genuine safety regression the theme makes reachable* ([amendment item 1](../v0.3.4-plan-amendment-2026-05-24.md#what-changes)). `EstimateCost` returns `$0` for any model absent from the pricing table ([`internal/cost/config.go:138`](../../internal/cost/config.go), "graceful degradation"), and the RFC 0023 pre-call lease keys off the configured model — so an "Any Model" alias with no price silently disables the structural cost gate for that agent. This PR adds a startup guard **scoped to the alias surface**: a resolved alias with no pricing entry produces a loud startup warning and **fails closed for non-local providers**, rather than a silent `$0` estimate. The deliberately-preserved raw-ID fall-through ([§E](0033-model-alias-layer.md#e-backwards-compatibility)) is left untouched — that is the back-compat half-step Phase 3 closes.

#### Scope

| File | Change |
|------|--------|
| `agents/model_aliases.py` | At alias-map load, validate that every alias entry carries pricing. A non-local alias (`provider` not in the local set — `mock` / `ollama` / OpenAI-compatible `base_url` pointing at localhost) with no `input_per_1m_tokens` / `output_per_1m_tokens` → loud startup warning + **fail closed** (`SystemExit` naming the alias and provider). A local ($0-by-design) alias is **distinguishable** from an unpriced one: an explicit `0`/simulation price or an explicit `local: true`-style marker on the entry is allowed silently; *absence* of pricing on a non-local entry is the error. The guard does **not** touch the `resolve()` raw-ID fall-through path. |
| [`config/optimization.yaml`](../../config/optimization.yaml) | Confirm the local/offline aliases carry the explicit marker the guard keys on (set up by PR 1's local-pricing decision) so the guard distinguishes "$0 on purpose" from "forgot the price." |
| `tests/unit/python/test_model_aliases.py` | The guard fires (loud warning + fail-closed) on an unpriced **non-local** alias; a local `$0`-by-design alias passes silently; a priced non-local alias passes; the raw-ID fall-through is **not** subject to the guard (a raw ID with no price still warns-and-degrades per [§E](0033-model-alias-layer.md#e-backwards-compatibility), unchanged). |

#### Key implementation details

- **Scoped to the alias surface, by design.** [Amendment §Phase 2 additions](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions): the guard closes the silent-$0 path *for the alias surface only*. The raw-ID fall-through keeps its graceful-degradation behaviour because that path is the deliberate back-compat half-step Phase 3 retires — closing it here would conflate two decisions. The Go-side `EstimateCost` $0 return ([`internal/cost/config.go:138`](../../internal/cost/config.go)) is the downstream symptom; the fix lands at the alias-resolution boundary where the locality and pricing are both known, not in the Go cost path.
- **Local vs unpriced is the load-bearing distinction.** A genuinely-$0 local model (Ollama / mock) is a legitimate configuration; an Anthropic/OpenAI alias with no price is a bug that disables the lease gate. The guard must tell them apart — hence the explicit local marker / explicit `0` price from PR 1's decision, so *absence* of pricing is unambiguously the error case.
- **Independent of the alias mechanics.** [Amendment §Risk additions](../v0.3.4-plan-amendment-2026-05-24.md#risk-additions): "it can land as a small standalone fix inside this release." It depends only on PR 1's map (to have aliases and their providers to validate), not on PR 2's factory rewrite or PR 3's config migration — hence it can land in parallel.

#### Tests

- Unpriced non-local alias → loud warning + `SystemExit`; priced non-local alias → passes; local $0-by-design alias → passes silently.
- Raw-ID fall-through with no price → unchanged graceful-degradation warning (not the new fail-closed path).

#### PR checklist

- [x] `pytest tests/unit/python/test_missing_price_guard.py tests/unit/python/test_model_aliases.py -q` passes (the guard tests are split into their own module to keep `test_model_aliases.py` under the file-size cap — guard fires on unpriced non-local; local $0 distinguishable; raw-ID path untouched; a well-formed resolve is **not** broken by an unrelated unpriced alias — `test_unrelated_unpriced_alias_does_not_break_a_good_resolve`).
- [x] `cd agents && mypy .` clean (whole-package, as CI runs it); `ruff check .` clean.
- [x] `make test` clean — verified via the touched/adjacent unit modules (`test_missing_price_guard.py`, `test_model_aliases.py`, `test_llm_factory.py`, `test_llm_client.py`, `test_optimization.py`, `test_optimization_routing.py`, `test_sub_agent_model_default.py`, `test_llm_ollama.py`, `test_llm_offline.py` — 180 passed) + mypy/ruff + `make validate`; no Go change (the guard lands at the Python alias-resolution boundary, not the Go cost path); CI runs the full target.
- [x] Guard is scoped to the alias surface — `resolve()`'s raw-ID fall-through ([§E](0033-model-alias-layer.md#e-backwards-compatibility)) is **not** failed closed (`test_raw_id_fall_through_not_failed_closed`).
- [x] A resolved non-local alias with no price warns loudly + fails closed; a local ($0-by-design) alias is distinguishable from an unpriced one ([amendment §Acceptance additions](../v0.3.4-plan-amendment-2026-05-24.md#acceptance-additions)). Locality keys on the provider name (`mock` / `ollama`) **or** a loopback `provider_config.base_url`. The guard fires **per-resolve, scoped to the resolved alias** (a config-backed alias is checked the first time it is resolved, before its first LLM call — so an unrelated, unused misconfigured entry never breaks a well-formed resolve); `validate_alias_pricing()` validates the whole map for an explicit startup / CI sweep.
- [x] [Progress Overview](#progress-overview) row 4 filled.

---

### PR 5: `feature/v034-rfc0033-telemetry-pricing` — `model_alias` Span Attribute + Alias-Derived Pricing + Cost Gate

**Depends on**: PR 2 (resolution wired) **and** PR 3 (configs on aliases) merged — so the cost gate exercises a genuinely alias-routed agent.
**Purpose**: Land Phase 2's telemetry and pricing-derivation, and the plan-level cost-attribution gate. Emit `persatrix.llm.model_alias` as a span attribute when a request came in via an alias ([RFC §G](0033-model-alias-layer.md#g-telemetry)); generate the legacy `cost.pricing.models.<id>` block from the alias map at config-load time and drop the duplicated literal pricing entries ([RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias)); assert an alias-routed agent reports correctly-keyed, non-zero cost via `GET /api/v1/cost/summary`. The alias-derived pricing carries OpenAI rows ([amendment item 2](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)).

#### Scope

| File | Change |
|------|--------|
| [`agents/llm_client.py`](../../agents/llm_client.py) | In the `agent.llm.call` span (around lines 244–250, alongside the existing `gen_ai.*` attributes), set the new optional `persatrix.llm.model_alias` attribute when the request resolved via an alias (`resolved.alias is not None`); omit it on the raw-ID path. The `gen_ai.request.model` attribute keeps the **physical** model ID per the [RFC 0019](0019-opentelemetry-completion.md) contract — the alias is *added*, never substituted ([RFC §G](0033-model-alias-layer.md#g-telemetry)). The `persatrix.*` prefix is mandated by [RFC 0019 §Attribute Schema](0019-opentelemetry-completion.md). |
| [`agents/optimization.py`](../../agents/optimization.py) / [`config/optimization.yaml`](../../config/optimization.yaml) | Generate the legacy `cost.pricing.models.<physical_id>` block from the alias map at config-load time ([RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias)); drop the duplicated literal `cost.pricing.models` entries (lines 40–46) so pricing stays in lock-step with the alias map automatically. The Go cost pipeline ([`internal/cost/`](../../internal/cost/)) reads pricing keyed by physical model ID off telemetry and is **unchanged** — it consumes the generated block exactly as before. |
| `tests/unit/python/test_optimization.py` | Assert the derived `cost.pricing.models` block matches the alias map (Anthropic + OpenAI physical IDs both present with their rows); the duplicated literal block is gone. |
| Cost-attribution integration test | **Cost gate** — an alias-routed agent (live or recorded) reports correctly-keyed, **non-zero** cost via `GET /api/v1/cost/summary` ([`internal/server/cost_handlers.go`](../../internal/server/cost_handlers.go)), and the `agent.llm.call` span carries `persatrix.llm.model_alias`. This is the [master-plan §Acceptance](../v0.3.4-plan.md#acceptance-for-v034) gate that the v0.3.2 cost surface does not regress across the re-keying. |

#### Key implementation details

- **The alias is added to telemetry, never substituted.** [RFC §G](0033-model-alias-layer.md#g-telemetry) + [RFC 0019](0019-opentelemetry-completion.md): `gen_ai.request.model` must carry the physical model ID so vendor backends render Persatrix traces unchanged. `persatrix.llm.model_alias` is a *new optional* attribute, populated only on alias-routed requests, so dashboards can roll up by logical role *or* drill down by physical ID. The existing `persatrix.llm.cache.hit` attribute is the precedent for the `persatrix.llm.*` sub-namespace.
- **Pricing derivation removes the duplication that caused mis-attribution risk.** Today pricing lives twice — once implicitly in the alias entry (PR 1) and once in `cost.pricing.models` literal keys. [RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias) resolves it by *generating* the legacy block from the alias map at load, so a migration that changes the `quality` alias's physical model automatically re-keys the price. This is the structural fix behind the [master-plan §Risk](../v0.3.4-plan.md#risk-and-mitigations) "a missed entry silently mis-attributes cost" row.
- **The Go side does not change.** [RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias) + [RFC §Files Touched](0033-model-alias-layer.md#files-touched-estimated): the cost pipeline reads pricing keyed by physical model ID off telemetry, which the resolver continues to emit. The derivation happens at config-load on the config-owning side; the Go orchestrator consumes the same `cost.pricing.models` shape it always has.
- **OpenAI rows ship here** ([amendment item 2](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)). Because the table is derived from the alias map and PR 1's map carries a priced OpenAI alias, the generated `cost.pricing.models` block includes the OpenAI physical ID's rows — so the master-plan Phase 4 one-line-swap test resolves to a priced target rather than tripping the PR 4 missing-price guard.

#### Tests

- Span: alias-routed request sets `persatrix.llm.model_alias`; raw-ID request omits it; `gen_ai.request.model` is the physical ID in both cases.
- Derived pricing: `cost.pricing.models` generated from the alias map (Anthropic + OpenAI present); literal duplication removed.
- Cost gate: alias-routed agent reports correctly-keyed, non-zero cost via `GET /api/v1/cost/summary`.

#### PR checklist

- [x] `pytest tests/unit/python/test_optimization.py tests/unit/python/test_llm_client.py -q` passes; the cost-attribution gate (`internal/server` + `internal/cost`) passes.
- [x] `cd agents && mypy .` clean (whole-package, as CI runs it); `ruff check .` clean (agents + tests).
- [x] `make test` clean — verified via the touched/adjacent Python modules + every `create_message` call-arg-asserting module + the offline/Ollama regression (no leaked `model_alias` kwarg) + `go test ./internal/cost/... ./internal/server/...` (the Go cost pipeline consumes the derived `cost.pricing.models` block unchanged — no Go source change) + `make validate`; CI runs the full target.
- [x] `persatrix.llm.model_alias` emitted on alias-routed requests only (the span attribute is set from the resolved alias, omitted on the §E raw-ID path); `gen_ai.request.model` stays the physical ID ([RFC §G](0033-model-alias-layer.md#g-telemetry) / [RFC 0019](0019-opentelemetry-completion.md)). The alias is telemetry-only and never forwarded to the provider call.
- [x] `cost.pricing.models` derived from the alias map (`agents/optimization.py` `derived_cost_pricing()`); the hand-maintained literal block is replaced by the projection (Anthropic `claude-sonnet-4-6` + Haiku + OpenAI `gpt-4o` rows; retired `claude-sonnet-4-20250514` dropped), pinned in lock-step by a drift guard ([RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias)).
- [x] **Cost-attribution gate green** — an alias-routed agent (usage keyed by the physical `claude-sonnet-4-6` the `quality` alias resolves to) reports correctly-keyed, non-zero cost via `GET /api/v1/cost/summary`, loading the real shipped config ([master-plan §Acceptance](../v0.3.4-plan.md#acceptance-for-v034)). This closes the PR 3 cost-regression row.
- [x] [Progress Overview](#progress-overview) row 5 filled.

#### Review findings

- [ISSUE-0072](../issues/ISSUE-0072-memory-compression-hardcoded-model-literals.md) — spun out of the PR 5 review. The three memory-compression LLM surfaces (`agents/memory/working.py:63`, `episodic_retention.py:45`, `episodic.py:413`) still hardcode raw vendor model IDs as parameter defaults (`claude-haiku-4` / `claude-haiku-4-5`) rather than routing through the alias layer — the last model-identity literals in production Python, and stale (the shipped Haiku id is `claude-haiku-4-5-20251001`). Out of scope here (those surfaces are on the §E raw-ID path, so PR 5 correctly emits no `model_alias` for them); a candidate fold-in for the RFC 0023 PRs 4–6 cost-path migration of these un-leased origins, since the re-keying only bites once the calls are counted ([ISSUE-0063](../issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md)).

---

### PR 6: `feature/v034-rfc0033-docs-sweep` — Documentation Sweep

**Depends on**: PR 3 merged (aliases live in config).
**Purpose**: Replace literal vendor IDs with alias examples across the docs that reference them ([RFC Phase 2 deliverable #3](0033-model-alias-layer.md#phase-2--telemetry--pricing-table-derivation)), so the documentation stops carrying the coupling the alias layer removes.

#### Scope

| File | Change |
|------|--------|
| [`docs/persatrix-extension-spec.md`](../persatrix-extension-spec.md) | Replace literal vendor IDs with alias examples ([RFC §Related Documentation](0033-model-alias-layer.md#related-documentation) — "extensive use of literal vendor IDs"). |
| [`docs/guides/persona-agents.md`](../guides/persona-agents.md) | Replace the `claude-sonnet-4-20250514` / `claude-haiku-4-5` pricing examples with alias references. |
| [`docs/ai-agents-orchestration-spec.md`](../ai-agents-orchestration-spec.md) | Replace the fallback-chain literal-vendor-ID examples with alias references. |
| RFC examples carrying literal vendor IDs | Sweep the RFC *config-coupling* examples (agent `model:` fields a reader copies) to alias form: [`0005-persona-agent-memory.md`](0005-persona-agent-memory.md) (×4 → `quality`), per [RFC Phase 2 deliverable #3](0033-model-alias-layer.md#phase-2--telemetry--pricing-table-derivation). **Not** swept — examples where the physical id *is* the subject (deliberate physical-ID discussion, per the §Tests carve-out): [`0004-python-agent-grpc-server.md`](0004-python-agent-grpc-server.md) / [`0004-pr-plan.md`](0004-pr-plan.md) `_create_provider` raw-ID→provider *inference* demos (the [§E](0033-model-alias-layer.md#e-backwards-compatibility) fall-through they show is preserved through Phase 2; an alias would void the demo), and [`0013-legal-ethical-compliance.md`](0013-legal-ethical-compliance.md) `ContentProvenance.model` (records the *actual* physical model, not a logical role). |
| Manual-test surface | [`MT-CHANNEL-004.md`](../manual-tests/MT-CHANNEL-004.md) prose claiming `ember-owl` "runs on `claude-sonnet-4-20250514`" → the `quality` alias (was stale; the shipped config flipped to `quality` in PR 3). **Not** swept — [`MT-MEMORY-003.md`](../manual-tests/MT-MEMORY-003.md) / [`manual-tests/README.md`](../manual-tests/README.md): see the deviation note below. (The *new* alias-routing MT is authored in the master-plan Phase 4, not here.) |

#### Key implementation details

- **Doc-only; no code or config change.** This PR touches prose and examples. The alias machinery and the migrated configs already shipped (PRs 1–3); this PR removes the *documentation* coupling so a reader copying an example gets an alias, not a soon-to-retire vendor ID.
- **Sweep vs. de-stale, by surface.** Config-coupling examples (agent `model:`, routing defaults, fallback chains, optimization profiles, sub-agent templates) → alias (`quality` / `fast` / `summarizer`). Surfaces where the physical id is the RFC-mandated value — the telemetry span ([RFC 0019](0019-opentelemetry-completion.md): `gen_ai.request.model` stays physical) and the `cost.pricing.models` keys ([RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias): keyed by physical id, derived from the alias map) — keep a physical id but the **retired** `claude-sonnet-4-20250514` is de-staled to the current `claude-sonnet-4-6`, and the extension-spec span gains the new `persatrix.llm.model_alias` attribute to show the §G rollup. The `SubAgentRequest.model` dataclass default in the specs is aligned to the shipped `str | None = None` (PR 3 / [RFC §J.3](0033-model-alias-layer.md#j-persona-and-sub-agent-model-selection)).
- **MT-MEMORY-003 / README deviation — deliberately NOT swept (deviates from [RFC §Test Strategy](0033-model-alias-layer.md#test-strategy)).** The RFC's Phase-2 plan to sweep `MT-MEMORY-003`'s `claude-haiku-4-5` references assumed the memory-compression model would be aliased by now. It is not: [ISSUE-0072](../issues/ISSUE-0072-memory-compression-hardcoded-model-literals.md) (deferred to the RFC 0023 cost-path migration) records that `agents/memory/working.py`'s `compression_model` default is **still a raw vendor id**. Those MT references are (a) dated execution-result logs (frozen historical records) and (b) accurate descriptions of that un-migrated code — sweeping them to `fast` would assert the code uses an alias, which is false. They sweep when ISSUE-0072 lands. Same rationale for the `manual-tests/README.md` result-table rows.
- **The new alias-routing manual test is master-plan Phase 4, not here.** [Master-plan Phase 4 PR 1](../v0.3.4-plan.md#phase-4--v034-release-prep-execution) authors and executes the MT that exercises an alias-routed agent (plus offline / Ollama / the one-line swap). This PR only sweeps *existing* doc/MT references off literal IDs.

#### Tests

- `make rfcs-check` / `make issues-check` / link-check pass (doc-link integrity).
- No literal `claude-sonnet-4-20250514` / `claude-haiku-4-5-…` remains in the swept docs except where a doc deliberately discusses physical IDs (e.g. the alias map's own example).

#### PR checklist

- [x] Swept docs reference aliases, not literal vendor IDs (except deliberate physical-ID discussion — telemetry/pricing keys keep the *current* physical id; closed-RFC inference/provenance demos and the un-migrated MT-MEMORY-003 surface keep theirs, see Key implementation details).
- [x] `make rfcs-check` + doc-link integrity pass (`doc_links.py`: 5281 links across 287 files OK; `issues-check`, `doc_status_markers.py`, `doc_audit.py`, `file_size.py --strict` all clean); `make test` not re-run — doc-only, no code or config touched, so the suite is unaffected.
- [x] The new alias-routing MT is **not** authored here (deferred to [master-plan Phase 4 PR 1](../v0.3.4-plan.md#phase-4--v034-release-prep-execution)).
- [x] [Progress Overview](#progress-overview) row 6 filled.

---

### PR 7: `feature/v034-rfc0033-close` — Phases 1–2 Closeout

**Depends on**: PR 6 merged (all Phase 1–2 implementation + docs complete). Any review-follow-up PRs merged first.
**Purpose**: Mark RFC 0033 partially implemented through Phase 2. Phase 3 (raw-ID pass-through removal + `_infer_provider` retirement) stays open with its observed-traffic gate per [§Future Phases](#future-phases) — a partial-RFC closeout, mirroring the [RFC 0024 PR 6](0024-pr-plan.md) precedent, since the full-RFC closeout waits for Phase 3 in v0.3.5+.

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0033-model-alias-layer.md`](0033-model-alias-layer.md) | Status → `⚠️ Partially Implemented (Phases 1–2)`. Append an "Implemented in v0.3.4" note to Decision/Next Steps; Phase 3 stays scheduled per the RFC's [§Phased Implementation Plan](0033-model-alias-layer.md#phased-implementation-plan). |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0033 row → `⚠️ Partially Implemented (Phases 1–2)`; target column stays `v0.3.4 (Phases 1–2) + v0.3.5+ (Phase 3)`; add merged-PR rows for PRs 1–7; `Last updated` refresh. |
| [`docs/rfcs/0033-pr-plan.md`](0033-pr-plan.md) | [Progress Overview](#progress-overview) rows filled with merged-PR numbers and dates; all checklists complete. |
| [`docs/v0.3.4-plan.md`](../v0.3.4-plan.md) | [Master Progress Overview](../v0.3.4-plan.md#master-progress-overview) row 2 → ✅ Merged with the workstream's first and final merge dates. |

No code changes; doc-only. `CHANGELOG.md` is **deferred to the v0.3.4 release process** ([master-plan Phase 3 / 4](../v0.3.4-plan.md#phase-3--v034-release-prep-plan)), mirroring the [RFC 0024 PR 6 precedent](0024-pr-plan.md).

#### PR checklist

- [ ] RFC 0033 status = `⚠️ Partially Implemented (Phases 1–2)`.
- [ ] [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) updated; merged-PR history includes PRs 1–7.
- [ ] [v0.3.4-plan Master Progress Overview](../v0.3.4-plan.md#master-progress-overview) row 2 → ✅ Merged.
- [ ] `make test`, `make lint`, `make validate` pass (doc-only change confirms no regression).
- [ ] [Progress Overview](#progress-overview) row 7 filled.

---

## Future Phases

Out of scope for v0.3.4; recorded here so future readers see the full RFC arc without re-reading the RFC body.

- **Phase 3 — v0.3.5+**: remove the raw-ID fall-through in `resolve()`; delete `_infer_provider` ([`agents/llm_client.py:371`](../../agents/llm_client.py)) and the `provider_inference` config block; schema bump to `"0.3"`; the loader rejects raw vendor IDs in `agents.yaml` with a clear error ([RFC §Phase 3](0033-model-alias-layer.md#phase-3--pass-through-removal--_infer_provider-retirement)). **Gated on observed traffic, not a calendar date**: Phase 3 opens only when the `persatrix.llm.alias.raw_id_usage{agent_id}` counter (added in PR 2) reads zero across the dogfood window — the project preference against pre-production calendar gates ([RFC §Non-Goals](0033-model-alias-layer.md#non-goals)). The Phase 1 startup deprecation warning (PR 2) plus that counter are the gate's signal; no new dashboard or metric pipeline is required.

No PR rows for Phase 3 in this plan — it lands in a v0.3.5+ PR plan (separate file) once the zero-raw-ID gate is met. Cross-link from that future plan to this one when it opens.

The [provider-parity amendment](../v0.3.4-plan-amendment-2026-05-24.md#deferred--named-not-scoped-into-v034) also names two items explicitly deferred (not Phase 3): a CLI `--model` / `--provider` override (re-homes to v0.3.5, since the user-facing surface carries no provider affordance today), and turning the `_infer_provider` unknown-model fall-through into a clear up-front error (small enough to ride a Phase 1 PR if convenient; not a gate).

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The migration re-keys pricing from raw model ID to alias; a missed entry silently mis-attributes cost across the v0.3.2 wallet/cost surface. | PR 5 *derives* the legacy `cost.pricing.models` block from the alias map at load ([RFC §F](0033-model-alias-layer.md#f-pricing-keyed-by-alias)), so pricing stays in lock-step automatically; the cost gate (`GET /api/v1/cost/summary` correctly-keyed, non-zero for an alias-routed agent) is a PR 5 release-blocker. |
| "Any Model" invites unpriced models; `EstimateCost` returns `$0` for them ([`internal/cost/config.go:138`](../../internal/cost/config.go)), silently disabling the RFC 0023 lease gate for that agent. | PR 4 adds a missing-price guard scoped to the alias surface: warn loudly + fail closed for non-local providers, while a local $0-by-design alias is distinguishable. Independent of the alias mechanics, so it can land in parallel with PR 2/PR 3. |
| The `create_provider` rewrite (PR 2) could regress the already-merged offline / Ollama modes before anyone re-runs them. | PR 2 keeps the force-flag short-circuits first and adds an offline/Ollama interplay regression test ([#422](https://github.com/mkhomutov/Persatrix/pull/422) / [#423](https://github.com/mkhomutov/Persatrix/pull/423)); the master-plan Phase 4 re-runs both end-to-end on the RC tip. |
| If v0.3.4 slips past 2026-06-15, default agents break on the Sonnet retirement. | The migration is a single `quality` alias edit — the smallest critical-path item (PR 1 already points `quality` at `claude-sonnet-4-6`; PR 3 points agents at `quality`). As a standalone fallback, a direct `claude-sonnet-4-20250514 → claude-sonnet-4-6` config swap clears the deadline independently of Phases 2+ ([RFC §Decision](0033-model-alias-layer.md#decision--next-steps)). |
| A genuinely-$0 local alias makes the simulated wallet never trip, contradicting the README's "agent pauses itself at the cap." | PR 1 records the local-pricing decision (explicit simulation price vs documented $0); if local stays $0, the wallet-cap demo is pinned to a priced alias ([amendment item 6](../v0.3.4-plan-amendment-2026-05-24.md#phase-2-additions)). The PR 4 guard keys on this decision to tell $0-on-purpose from forgot-the-price. |
| Phase 1 keeps a raw-ID fall-through; teams could ignore the warning and the abstraction never fully lands. | Phase 3 (raw-ID rejection) closes it, gated on the PR 2 `raw_id_usage` counter reading zero — observed traffic, not a date. The startup warning + counter are that gate's signal. |
| This plan rots as PRs 1–7 land. | Each PR's checklist updates the [Progress Overview](#progress-overview) and the [v0.3.4-plan Master Progress Overview](../v0.3.4-plan.md#master-progress-overview); the [ROADMAP Hygiene](#roadmap-hygiene) rules below are part of every PR. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md) and [v0.3.4-plan §ROADMAP hygiene](../v0.3.4-plan.md#roadmap-hygiene):

- **This PR-plan PR opens / merges** → no RFC 0033 status change — authoring a PR plan does not start implementation; RFC 0033 stays `📋 Proposed`. The [RFC Master Index](../../ROADMAP.md#rfc-master-index) *target* already reads `v0.3.4 (Phases 1–2) + v0.3.5+ (Phase 3)` (set by the [master-plan Phase 0 PR](../v0.3.4-plan.md#phase-0--this-pr)); this PR flips the RFC 0033 frontmatter `target:` to match and cross-links this plan from the RFC. [§Version Map](../../ROADMAP.md#version-map) v0.3.4 row stays `🚧 Planning`.
- **PR 1 opens / merges** → no status change (resolver substrate, unconsumed). RFC 0033 stays `📋 Proposed`.
- **PR 2 opens** → RFC 0033 row → `🚧 Implementing` (first PR that wires the resolver into the runtime); [v0.3.4-plan Master Progress Overview](../v0.3.4-plan.md#master-progress-overview) row 2 → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview) row with the PR number and date.
- **PR 7 merges** → RFC 0033 row → `⚠️ Partially Implemented (Phases 1–2)`; [v0.3.4-plan Master Progress Overview](../v0.3.4-plan.md#master-progress-overview) row 2 → ✅ Merged; `Last updated` refresh.

---

## Progress Overview

| # | RFC Phase | Title | Branch | Status | GitHub PR | Merged |
|---|-----------|-------|--------|--------|-----------|--------|
| 1 | 1 | Resolver module + `models.aliases` config block (+ OpenAI alias, local-pricing decision) | `feature/v034-rfc0033-resolver` | ✅ Merged | [#431](https://github.com/mkhomutov/Persatrix/pull/431) | 2026-05-25 |
| 2 | 1 | `create_provider` tuple return + §D precedence + offline/Ollama interplay regression + raw-ID startup warning + `raw_id_usage` counter | `feature/v034-rfc0033-factory` | ✅ Merged | [#432](https://github.com/mkhomutov/Persatrix/pull/432) | 2026-05-25 |
| 3 | 1 | Config migration to aliases (Sonnet 4→4.6 via `quality`) + network-allowlist neutralization + `SubAgentRequest.model` `None`-default + §J resolution | `feature/v034-rfc0033-migration` | ✅ Merged | [#433](https://github.com/mkhomutov/Persatrix/pull/433) | 2026-05-26 |
| 4 | 2 | Missing-price guard — fail-closed for unpriced non-local aliases ([amendment item 1](../v0.3.4-plan-amendment-2026-05-24.md#what-changes)) | `feature/v034-rfc0033-missing-price-guard` | ✅ Merged | [#434](https://github.com/mkhomutov/Persatrix/pull/434) | 2026-05-26 |
| 5 | 2 | `persatrix.llm.model_alias` span attr + alias-derived pricing + `/cost/summary` cost gate (+ OpenAI rows) | `feature/v034-rfc0033-telemetry-pricing` | ✅ Merged | [#435](https://github.com/mkhomutov/Persatrix/pull/435) | 2026-05-26 |
| 6 | 2 | Documentation sweep — replace literal vendor IDs with alias examples | `feature/v034-rfc0033-docs-sweep` | 🔀 PR open | [#436](https://github.com/mkhomutov/Persatrix/pull/436) | — |
| 7 | — | Phases-1–2 closeout | `feature/v034-rfc0033-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

> Review-follow-up PRs, if review surfaces findings, slot in before PR 7 following the [RFC 0024 PR 5 / PR 5.1](0024-pr-plan.md) precedent; they are not pre-numbered here.

---

## Related Documentation

- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md) — canonical spec.
- [v0.3.4-plan.md](../v0.3.4-plan.md) — master plan; row 2 of the Master Progress Overview is this workstream.
- [v0.3.4 plan amendment 2026-05-24](../v0.3.4-plan-amendment-2026-05-24.md) — provider-parity hardening; the Phase 2 additions (missing-price guard, OpenAI peer, network-allowlist, local-pricing) fold into PRs 1 / 4 / 5 here.
- [RFC 0024 PR plan](0024-pr-plan.md) — structural template (partial-RFC closeout + substrate-first sequencing + review-follow-up shape).
- [RFC 0004 — Python Agent gRPC Server](0004-python-agent-grpc-server.md) — the `LLMProvider` Protocol the alias layer sits above (untouched).
- [RFC 0008 — Memory & Context Optimization](0008-agent-memory-context-optimization.md) — established [`config/optimization.yaml`](../../config/optimization.yaml) as the routing/cost config home.
- [RFC 0019 — OpenTelemetry Completion](0019-opentelemetry-completion.md) — `gen_ai.*` span conventions + the `persatrix.*` reserved-namespace rule the new `persatrix.llm.model_alias` attribute is additive within.
- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — the pre-call lease gate PR 4 protects and the `GET /api/v1/cost/summary` surface PR 5's cost gate defends.
- Already-merged provider work this release ships: offline `MockProvider` ([#422](https://github.com/mkhomutov/Persatrix/pull/422)), Ollama ([#423](https://github.com/mkhomutov/Persatrix/pull/423)) — PR 2's interplay regression keeps both green.
- [Anthropic Model Deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations) — vendor source of truth for the Sonnet 4 retirement (2026-06-15) the `quality` alias edit absorbs.
- [`agents/llm_client.py`](../../agents/llm_client.py) — `create_provider` / `_infer_provider`, rewritten in PR 2; `_infer_provider` retired in Phase 3.
- [`agents/persona_types.py`](../../agents/persona_types.py) — `SubAgentRequest.model`, the last runtime model literal, dropped to `None` in PR 3.
