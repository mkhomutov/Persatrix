---
id: RFC-0033
title: Provider-Agnostic Model Alias Layer
summary: Decouple agent configs from vendor-specific model IDs by routing every model reference through a single alias map, so vendor deprecations and multi-provider expansion change one file instead of dozens.
type: architecture
status: partially_implemented
author: Maksim Khomutov
created: 2026-05-15
target: v0.3.4 (Phases 1–2) + v0.3.5+ (Phase 3)
depends_on:
  - RFC-0004
  - RFC-0008
---

# RFC 0033 — Provider-Agnostic Model Alias Layer

**Type**: architecture
**Status**: ⚠️ Partially Implemented (Phases 1–2) — all seven Phases-1–2 PRs merged ([#431](https://github.com/mkhomutov/Persatrix/pull/431)–[#436](https://github.com/mkhomutov/Persatrix/pull/436) + closeout). Tracking plan: [0033-pr-plan.md](0033-pr-plan.md). Phase 3 (raw-ID pass-through removal + `_infer_provider` retirement) stays observed-traffic gated for v0.3.5+.
**Author**: Maksim Khomutov
**Date**: 2026-05-15
**Target**: v0.3.4 (Phases 1–2) + v0.3.5+ (Phase 3)
**PR plan**: [0033-pr-plan.md](0033-pr-plan.md) (Phases 1–2 — the v0.3.4 contract)
**Depends on**: RFC 0004 (Python Agent gRPC Server — established the `LLMProvider` Protocol), RFC 0008 (Memory & Context Optimization — established `optimization.yaml` as the cost/routing config home)
**Relates to**: RFC 0006 (Efficiency & Execution Limits — cost pricing tables)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Vocabulary](#a-vocabulary)
  - [B. Config Shape](#b-config-shape)
  - [C. Resolver](#c-resolver)
  - [D. Factory Integration](#d-factory-integration)
  - [E. Backwards Compatibility](#e-backwards-compatibility)
  - [F. Pricing Keyed by Alias](#f-pricing-keyed-by-alias)
  - [G. Telemetry](#g-telemetry)
  - [H. Multi-Provider Extensibility](#h-multi-provider-extensibility)
  - [I. Retirement of `_infer_provider`](#i-retirement-of-_infer_provider)
  - [J. Persona and Sub-Agent Model Selection](#j-persona-and-sub-agent-model-selection)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Persatrix agent configs reference concrete vendor model IDs (`claude-sonnet-4-20250514`, `claude-haiku-4-5-20251001`) directly. The same literal string appears in [`config/agents.yaml`](../../config/agents.yaml) (6×), [`config/optimization.yaml`](../../config/optimization.yaml) (defaults *and* pricing keys), [`agents/persona_types.py`](../../agents/persona_types.py) as a dataclass default, and ~20 documentation files. Every vendor deprecation cycle requires a sweep across all of them, and the in-code `_infer_provider` heuristic in [`agents/llm_client.py`](../../agents/llm_client.py) decides routing by string prefix — a pattern that does not scale past two vendors.

This RFC proposes a **single source of truth** for model identity: a named alias map that resolves a logical role (`quality`, `fast`, `summarizer`) into a concrete `(provider, model_id, pricing)` record. Agent configs reference aliases. Migrations edit one map. New providers (Gemini, Mistral, local Ollama) attach by adding alias entries, not by extending a string-prefix table.

The active Anthropic [Sonnet 4 retirement](https://platform.claude.com/docs/en/about-claude/model-deprecations) (2026-06-15) is the proximate trigger; the alias layer absorbs that migration as its first real exercise rather than requiring a parallel one-off sweep.

## Motivation

### Today's coupling

Concrete model IDs leak through every layer:

| Layer | Example | Failure mode on vendor retirement |
|------|---------|-----------------------------------|
| Agent config | [`config/agents.yaml:11`](../../config/agents.yaml) — `model: "claude-sonnet-4-20250514"` (×6) | Agents stop working until each line is edited |
| Routing defaults | [`config/optimization.yaml:10-11`](../../config/optimization.yaml) — `task_agents: "claude-sonnet-4-…"` | Default profile breaks for new agents |
| Summarization model | [`config/optimization.yaml:36`](../../config/optimization.yaml) — `context_management.summarization.model: "claude-haiku-4-5-…"` | Summarization path silently breaks on a Haiku retirement until this separate field is edited |
| Pricing | [`config/optimization.yaml:41`](../../config/optimization.yaml) — pricing keyed by exact model ID | Cost reporting silently mis-attributes when the agent migrates but the pricing entry doesn't |
| Code default | [`agents/persona_types.py:118`](../../agents/persona_types.py) — `model: str = "claude-sonnet-4-…"` | Dataclass default points at a retired ID — needs a code change every cycle |
| Provider inference | [`agents/llm_client.py:232`](../../agents/llm_client.py) — `_infer_provider` matches string prefixes | Adding a third vendor inflates the heuristic; ambiguous prefixes (`mistral-*` vs `mistral-medium-latest` via a proxy) become judgement calls |
| Docs / specs | ~20 files | Doc rot accumulates each migration |

The `LLMProvider` Protocol abstraction (RFC 0004) was correct — the surface that's missing is **above** the provider, between the agent author and the vendor namespace.

### Vendor deprecation cycle

Anthropic publishes a 60-day-minimum notice and an explicit retirement date per model. As of 2026-05-15:

- `claude-sonnet-4-20250514` — **deprecated 2026-04-14, retires 2026-06-15** (recommended replacement: `claude-sonnet-4-6`).
- `claude-opus-4-20250514` — same window.
- `claude-haiku-4-5-20251001` — active, retirement not sooner than 2026-10-15.

The retirement is unavoidable. The question this RFC settles is whether each future deprecation costs a 30-file sweep or a one-line edit.

### Multi-vendor near-future

The user has flagged that the project will allow non-Anthropic, non-OpenAI APIs (Gemini, Mistral, local Ollama / LM Studio / vLLM). The current `_infer_provider` heuristic at [`agents/llm_client.py:232`](../../agents/llm_client.py) decides routing from the model string. With two vendors and orthogonal prefixes (`claude-` vs `gpt-`/`o[1-4]-`) this works. With four+ vendors and overlapping ranges (e.g., a local Ollama tag named `claude-3-haiku:Q4_K_M`) it falls apart. An alias map sidesteps the question entirely — provider is data, not inferred.

## Goals

1. **Single source of truth.** One file (`config/models.yaml` or a new section under `optimization.yaml`) lists every model the system can call, with provider, vendor ID, and pricing.
2. **Agent configs reference aliases, not vendor IDs.** `model: quality` instead of `model: claude-sonnet-4-20250514`.
3. **Migration is a one-line edit.** Changing the physical model behind `quality` requires editing exactly one map entry — no sweeps through agents.yaml, persona defaults, pricing tables, or docs.
4. **Provider is explicit, not inferred.** The alias entry declares the provider; `_infer_provider` is removed.
5. **Multi-vendor extensibility.** Adding a Gemini / Mistral / local-Ollama provider adds an alias entry; no heuristic-table edits.
6. **No agent code changes.** The `LLMProvider` Protocol surface and `LLMClient` facade are untouched. The change lives in the factory and the config layer.
7. **Backwards compatible during cutover.** Raw vendor model IDs in existing configs still work; the resolver passes them through unchanged. Hard-removal of the pass-through is a separate, future RFC step gated by data, not by this RFC.

## Non-Goals

- **Adding new providers.** Gemini / Mistral / local providers are out of scope for this RFC. The alias layer enables their later addition; their implementations are separate work.
- **Changing the `LLMProvider` Protocol** or `LLMClient` facade. Same `create_message` / `format_tool_definitions` / `append_tool_round` surface.
- **Capability tagging** (e.g., `tool_use: true`, `vision: true`, `streaming: true` per model). Useful, but a separable concern — defer.
- **Per-tenant or per-workflow alias overrides.** All consumers resolve against one map. Overrides can layer on later under the existing `active_profile` mechanism in [`config/optimization.yaml:5`](../../config/optimization.yaml).
- **Calendar-gated CI failure** on approaching vendor retirement dates. Soft startup warning is acceptable; a hard date-based block is not (per project preference against calendar-driven gates pre-production).
- **Rewriting historical telemetry.** Existing spans / metrics already carry the physical model string; that continues. The alias name is *added* as a separate attribute, not substituted.

## Design / Implementation

### A. Vocabulary

- **Alias** — a logical name (`quality`, `fast`, `summarizer`, `task-default`) that agents and config reference.
- **Alias entry** — the resolved record: `{provider, model, input_per_1m, output_per_1m, [notes]}`.
- **Resolver** — a function `resolve(alias_or_model: str) -> ResolvedModel` that maps an alias to its entry, or passes a raw vendor ID through unchanged (with provider inferred from the existing prefix table, preserved only for the deprecation window of raw IDs).

### B. Config Shape

Add a `models` block to `config/optimization.yaml` (keeps the migration small — no new file, no new loader). The block sits at top level alongside `default`, `cost`, etc.:

```yaml
# config/optimization.yaml (excerpt)
schema_version: "0.2"   # bump — alias block is the only schema change

models:
  aliases:
    quality:
      provider: anthropic
      model: claude-sonnet-4-6
      input_per_1m_tokens: 3.00
      output_per_1m_tokens: 15.00
    fast:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      input_per_1m_tokens: 0.80
      output_per_1m_tokens: 4.00
    summarizer:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      input_per_1m_tokens: 0.80
      output_per_1m_tokens: 4.00
    # Future providers attach by adding entries — no heuristic-table edits:
    # local-fast:
    #   provider: openai           # OpenAI-compatible adapter
    #   model: llama-3.1-8b
    #   provider_config:
    #     base_url: http://localhost:11434/v1
    #   input_per_1m_tokens: 0
    #   output_per_1m_tokens: 0

default:
  model_routing:
    defaults:
      task_agents: quality           # alias, not vendor ID
      sub_agents: quality
      evaluators: fast
    # provider_inference: retained for the cutover window only;
    # see §E (Backwards Compatibility) and §I (_infer_provider retirement).

  context_management:
    max_context_tokens: 80000
    summarization:
      enabled: true
      model: summarizer              # alias — was: "claude-haiku-4-5-20251001"
  ...
```

The `context_management.summarization.model` field at [`config/optimization.yaml:36`](../../config/optimization.yaml) is a third literal vendor-ID surface today (alongside the `default.model_routing.defaults` entries and the `cost.pricing.models` keys). Phase 1 migrates it to the `summarizer` alias in the same sweep — leaving it raw would undermine the "one edit, one alias entry" property the RFC is built on, because a Sonnet/Haiku swap on the summarization path would still require touching this line by hand.

Pricing duplication between the alias entry and the legacy `cost.pricing.models.<model_id>` block is resolved by **deriving the legacy block from aliases at load time** (Phase 2). Both shapes are accepted during cutover; the resolver emits the legacy shape for downstream consumers that haven't migrated yet.

### C. Resolver

```python
# agents/model_aliases.py  (new module — leaf, no agent-runtime imports)

@dataclass(frozen=True)
class ResolvedModel:
    alias: str | None          # None if input was a raw vendor ID
    provider: str              # "anthropic" | "openai"
    model: str                 # physical vendor ID, used in API call
    input_per_1m_tokens: float
    output_per_1m_tokens: float
    provider_config: dict      # passthrough — e.g. base_url for OpenAI-compat
    raw: bool                  # True if no alias matched and we fell through

def resolve(alias_or_model: str) -> ResolvedModel: ...
```

The resolver reads from a process-wide singleton populated by [`agents/optimization.py`](../../agents/optimization.py) at module load (same shape as the existing `provider_inference()` / `active_profile` accessors). Loading is lazy and cached; tests can override via a context-manager seam.

### D. Factory Integration

[`agents/llm_client.py:247`](../../agents/llm_client.py) `create_provider` becomes:

```python
def create_provider(agent_config: dict) -> tuple[LLMProvider, str]:
    """Returns (provider_instance, physical_model_id)."""
    requested = agent_config["model"]
    if not requested:
        raise SystemExit("Agent config 'model' field is empty")

    resolved = resolve(requested)
    # Caller writes resolved.model into the create_message(model=…) call,
    # so the API call still goes to the vendor ID, not the alias name.

    if resolved.provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        ...
        return AnthropicProvider(api_key=api_key), resolved.model
    elif resolved.provider == "openai":
        base_url = resolved.provider_config.get("base_url") \
                   or agent_config.get("provider_config", {}).get("base_url")
        ...
        return OpenAIProvider(api_key=..., base_url=base_url), resolved.model
    raise SystemExit(f"Unknown LLM provider: {resolved.provider!r}")
```

Callers of `create_provider` are updated to use the returned physical model ID for `create_message(model=…)`. Today there are ~3 caller sites — the planner agent factory, the persona runtime, and the summarization path.

**Precedence rules** (stated explicitly so the factory has no ambiguous-input branches):

1. **Alias-declared `provider` vs explicit `provider:` field on the agent entry.** When `model:` resolves to an alias, the alias's `provider` is authoritative; an explicit `provider:` field on the agent entry that disagrees is an **error** at config load (`SystemExit` with a clear message naming the agent ID, the alias, and both conflicting providers). Rationale: the alias *is* the joint declaration of provider + model + pricing; a redundant-and-agreeing `provider:` field would be ignored noise, and a disagreeing one indicates a config bug that should not silently resolve one way or the other. Today's `agent_config.get("provider") or _infer_provider(model)` precedence at [`agents/llm_client.py:257`](../../agents/llm_client.py) is preserved **only** on the raw-ID pass-through path (§E), where there's no alias to disagree with.
2. **Alias `provider_config` vs agent-entry `provider_config`.** Alias-level `provider_config` is authoritative for fields it declares; the agent entry's `provider_config` provides fallback values only for fields the alias leaves unset. Rationale: the alias represents shared, named infrastructure (e.g., the team's Ollama instance) and should not be silently overridden per agent; truly per-agent provider settings indicate the alias is wrong and should be split into two alias entries instead. The §D code sketch's `resolved.provider_config.get("base_url") or agent_config.get("provider_config", {}).get("base_url")` encodes this rule.

### E. Backwards Compatibility

The resolver accepts **either** an alias or a raw vendor model string. If no alias matches, the resolver falls through to the existing prefix table from [`config/optimization.yaml`](../../config/optimization.yaml) and returns a `ResolvedModel` with `alias=None`, `raw=True`. This means:

1. Existing configs that say `model: "claude-sonnet-4-20250514"` continue to work.
2. The Phase 1 PR can land the resolver and the alias map without forcing a config sweep in the same PR.
3. Phase 2 then migrates configs from raw IDs to aliases incrementally; CI emits a deprecation warning per raw-ID usage at startup.
4. Phase 3 (timing not gated by this RFC — driven by zero-raw-ID metric, not a calendar) removes the pass-through and `_infer_provider`.

### F. Pricing Keyed by Alias

Today's cost wiring in [`internal/cost/`](../../internal/cost/) (Go) reads pricing keyed by physical model ID — that comes off the LLM call telemetry, which carries the physical ID. **No change required on the Go side.** The resolver-side change is that the legacy `cost.pricing.models.<id>` block is *generated* from the alias map at config-load time, so:

- The pricing table stays in lock-step with the alias map automatically — no separate edit on migration.
- Operators who want bespoke pricing (e.g., for a discounted enterprise contract) can still override per-model in a separate `cost.pricing.overrides.<id>` block (out of scope to design here — placeholder for a future RFC if needed).

### G. Telemetry

OTEL Gen-AI semantic conventions ([RFC 0019 §D / §E](0019-opentelemetry-completion.md)) require `gen_ai.request.model` to carry the physical model ID — that contract is preserved. The alias name is added as a **new optional attribute** `persatrix.llm.model_alias`, populated only when the request came in via an alias. This lets dashboards and cost reporting roll up by alias (logical role) *or* drill down by physical ID, without breaking the existing vendor-neutral span contract.

The `persatrix.*` prefix (rather than extending `gen_ai.*`) is mandated by [RFC 0019 §Attribute Schema](0019-opentelemetry-completion.md): "*Persatrix-specific attributes use the reserved `persatrix.*` prefix; Gen-AI attributes use the upstream `gen_ai.*` prefix verbatim.*" The alias concept is Persatrix-specific (not part of upstream OTel Gen-AI conventions), so it belongs under `persatrix.*`. The `persatrix.llm.*` sub-namespace already has a precedent in RFC 0019's `persatrix.llm.cache.hit` metric attribute.

### H. Multi-Provider Extensibility

The alias entry's `provider` field is the only routing surface. Adding a provider (Gemini, Mistral, local Ollama, Vertex AI direct) means:

1. Add a new `Provider` class implementing the existing `LLMProvider` Protocol from [`agents/llm_types.py`](../../agents/llm_types.py) (mirrors `AnthropicProvider` / `OpenAIProvider` in [`agents/llm_providers.py`](../../agents/llm_providers.py)).
2. Add the provider's name to the `create_provider` branch in [`agents/llm_client.py`](../../agents/llm_client.py).
3. Add alias entries pointing at the new provider.

No heuristic-table maintenance, no per-vendor string-prefix rules, no `_infer_provider` extension. Each new provider is one new class + one factory branch + N alias entries.

### I. Retirement of `_infer_provider`

The string-prefix routing heuristic at [`agents/llm_client.py:232-244`](../../agents/llm_client.py) is preserved only for the duration of the raw-ID pass-through (§E). When raw-ID usage drops to zero (measured by the startup deprecation warning counter), the heuristic and the `provider_inference` config block are removed in a follow-up PR. Removal is **not** gated by a calendar date — it's gated by observed traffic.

### J. Persona and Sub-Agent Model Selection

Personas, task agents, and code-spawned sub-agents pick their model through three distinct paths today. All three converge on the alias surface under this RFC:

**1. Personas and task agents — uniform schema.** Personas live in [`config/agents.yaml`](../../config/agents.yaml) as entries with `type: "persona"` alongside `type: "task"` entries; both carry a top-level `model:` field with identical semantics ([`config/agents.yaml:98`](../../config/agents.yaml) `ember-owl` persona vs [`config/agents.yaml:11`](../../config/agents.yaml) `planner` task agent). **The `model:` field on any agent entry accepts an alias.** No type-level distinction. The resolver doesn't know or care whether the caller is a persona or a task agent — it returns the same `ResolvedModel` either way. Phase 1's sweep of `agents.yaml` covers both kinds of entries with one pass.

**2. Operators who want persona-specific knobs use alias naming, not schema.** If an operator wants a distinct dial for personas (e.g., "personas should run on a chattier model than task agents"), they declare a `persona-default` alias in `models.aliases` and reference it from persona entries:

```yaml
# config/optimization.yaml
models:
  aliases:
    quality:         { provider: anthropic, model: claude-sonnet-4-6, ... }
    persona-default: { provider: anthropic, model: claude-sonnet-4-6, input_per_1m_tokens: 3.00, output_per_1m_tokens: 15.00 }
    # ^ Today identical to `quality`; the operator can pivot one without affecting the other.

# config/agents.yaml
agents:
  - id: "ember-owl"
    type: "persona"
    model: persona-default      # operator chose a dedicated knob
```

The alias map has no special "persona" type — it's pure data. This keeps the schema uniform and the indirection composable.

**3. Code-spawned sub-agents — drop the literal default.** [`SubAgentRequest.model`](../../agents/persona_types.py) at line 118 currently carries a hardcoded vendor ID as a dataclass default. This is the *only* code-level model literal in the runtime; every other reference flows through config. Phase 1 changes it to:

```python
@dataclass
class SubAgentRequest:
    ...
    model: str | None = None     # was: "claude-sonnet-4-20250514"
    ...
```

The sub-agent factory resolves `None` at construction time to the active profile's `default.model_routing.defaults.sub_agents` alias (today's literal value migrates to the alias `quality` in the same PR — see §D). Callers that want a specific model can still pass an alias string (`SubAgentRequest(..., model="fast")`), but the *default* lives in config alongside every other routing knob. After Phase 1, **no Python runtime code carries a literal vendor model ID** — every model reference flows through either an alias (resolved at call time) or the raw-ID pass-through (config only, removed in Phase 3). Vendor IDs remain in config (one per alias entry), which is the intended single source of truth.

**Why not introduce a persona-only `behavior.model` field?** Two reasons. First, the existing `model:` at agent-entry top level already covers it — adding a second knob doubles the surface for no new capability. Second, it would break the "one source of truth per agent" principle: which wins, top-level `model:` or `behavior.model`? Resolving that requires precedence rules that the uniform-field design avoids entirely.

**Open Question** (added to the OQ section): should the **active profile** (cost / speed / quality / simulation per [`config/optimization.yaml:5`](../../config/optimization.yaml)) be able to override the alias a persona declares? E.g., if `ember-owl` declares `model: quality` but the operator sets `active_profile: cost_optimized`, does the persona silently downgrade? Tentative answer for Phase 1: **no override** — agent-declared aliases are authoritative; profiles only control the resolver's *defaults* table (the values referenced when an entry says `task_agents` / `sub_agents` / `evaluators`). Persona-by-persona profile overrides can layer in later if dogfood demands.

## Security Considerations

- **No new attack surface.** The alias map is a config file like any other; it doesn't introduce new network egress, new credential flows, or new permission gates.
- **Misrouting risk.** A wrong `provider:` value in an alias entry would route requests to the wrong vendor — but the request would fail at authentication (vendor SDK rejects the API key it doesn't recognize) before reaching the model. No silent data exfiltration vector beyond the existing one (operator can already misconfigure a raw model string today).
- **Config sprawl.** Aliases are a finite, named set. Operators can't conjure undeclared aliases at runtime — the resolver only knows what's in the config file. This is tighter than the current world (where any arbitrary string is accepted and silently routed via the prefix heuristic).
- **Audit trail.** The new `persatrix.llm.model_alias` span attribute (§G) makes the alias → physical-model mapping queryable from telemetry, so post-hoc audit of "which agents called which physical model under which logical role" is straightforward.

## Phased Implementation Plan

Each phase ships as an independent PR under the v0.3.x umbrella, branch prefix `feature/v03x-rfc0033-`. The full set is scoped to fit the standard 500-line soft cap per [BRANCHING.md](../BRANCHING.md).

### Phase 1 — Resolver + alias config block + first migration

**Deliverables**:

1. `agents/model_aliases.py` — new module: `ResolvedModel` dataclass + `resolve()` function + config loader.
2. `config/optimization.yaml` — add `models.aliases` block with `quality`, `fast`, `summarizer` entries pointing at `claude-sonnet-4-6` / `claude-haiku-4-5-20251001`; bump `schema_version` to `"0.2"`.
3. [`agents/llm_client.py`](../../agents/llm_client.py) `create_provider` — return `(provider, physical_model)` tuple, consume `resolve()` output.
4. Update all `create_provider` call sites to use the returned physical model.
5. Migrate [`config/agents.yaml`](../../config/agents.yaml) (6 entries — covers both `type: task` and `type: persona`; see §J) to alias references. The Anthropic Sonnet 4 → 4.6 migration is **absorbed by changing only the `quality` alias entry's `model:` field** — no per-agent sweep.
6. Migrate [`config/optimization.yaml`](../../config/optimization.yaml) `context_management.summarization.model` (line 36) from the raw Haiku ID to the `summarizer` alias. This is a third config-side literal not covered by items 2 or 5; without this step a Haiku retirement (RFC 0033 absorbs the next one) would still require touching this line by hand, defeating the "one alias edit" property.
7. Drop the hardcoded vendor ID from [`agents/persona_types.py:118`](../../agents/persona_types.py): `SubAgentRequest.model: str | None = None`; the sub-agent factory resolves `None` to the active profile's `default.model_routing.defaults.sub_agents` alias at construction time (§J). After Phase 1, no runtime code carries a literal vendor model ID.
8. Unit tests for the resolver (alias hit, raw-ID fallthrough, deprecation warning emission) and for the sub-agent factory's `None`-default resolution path (§J).
9. Deprecation warning at startup if any agent config uses a raw vendor model ID.

**Dependencies**: none external. Sits between RFC 0026 PRs in flight; no shared files.

### Phase 2 — Telemetry + pricing-table derivation

**Deliverables**:

1. Emit `persatrix.llm.model_alias` span attribute when a request came in via an alias ([`agents/llm_client.py`](../../agents/llm_client.py)) — see §G for namespace rationale.
2. Generate the legacy `cost.pricing.models.<id>` block from the alias map at config load time; drop the duplicated literal pricing entries from `config/optimization.yaml`.
3. Documentation sweep: update [`docs/persatrix-extension-spec.md`](../persatrix-extension-spec.md), [`docs/guides/persona-agents.md`](../guides/persona-agents.md), [`docs/ai-agents-orchestration-spec.md`](../ai-agents-orchestration-spec.md), and the RFC examples that show literal vendor IDs.

**Dependencies**: Phase 1 merged.

### Phase 3 — Pass-through removal + `_infer_provider` retirement

**Deliverables**:

1. Remove the raw-ID fallthrough in `resolve()`.
2. Delete `_infer_provider` and the `provider_inference` config block.
3. Schema bump to `"0.3"`; loader rejects raw vendor IDs in `agents.yaml` with a clear error.

**Dependencies**: Phase 2 merged **and** zero raw-ID startup warnings observed in dogfood (the gate is observed traffic, not a calendar date — per project preference against pre-production calendar gates).

**Gate metric source.** The "zero raw-ID usage" signal is produced by the Phase 1 startup deprecation warning (Phase 1 deliverable #9) plus a paired OTEL counter `persatrix.llm.alias.raw_id_usage{agent_id}` incremented once per agent at config load when a raw vendor ID is detected. Phase 3 is gated on this counter reading zero across the dogfood window — operators inspect it via the existing observability stack (the `persatrix.llm.*` metric family established by RFC 0019 §Attribute Schema). No new dashboard or metric pipeline is required; the counter rides the same export path as `persatrix.llm.cache.hit`.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Python agents | `agents/model_aliases.py` | **New** — resolver + dataclass |
| Python agents | [`agents/llm_client.py`](../../agents/llm_client.py) | `create_provider` returns `(provider, model)` tuple; consume resolver |
| Python agents | [`agents/optimization.py`](../../agents/optimization.py) | Load + expose `models.aliases` block |
| Python agents | [`agents/persona_types.py`](../../agents/persona_types.py) | Drop hardcoded default; resolver supplies it |
| Config | [`config/optimization.yaml`](../../config/optimization.yaml) | Add `models.aliases` block; rewrite `default.model_routing.defaults` to use aliases; rewrite `context_management.summarization.model` (line 36) to `summarizer` alias |
| Config | [`config/agents.yaml`](../../config/agents.yaml) | 6 `model:` fields → alias names |
| Tests | `tests/unit/python/test_model_aliases.py` | **New** — resolver coverage |
| Tests | [`tests/unit/python/test_llm_client.py`](../../tests/unit/python/test_llm_client.py) | Update for tuple return + alias path |
| Tests | [`tests/unit/python/test_optimization.py`](../../tests/unit/python/test_optimization.py) | `models.aliases` block parsing |
| Docs (Phase 2) | `docs/persatrix-extension-spec.md`, `docs/guides/persona-agents.md`, `docs/ai-agents-orchestration-spec.md` | Replace literal vendor IDs with alias examples |

Go orchestrator: **no change** — the cost pipeline reads pricing keyed by physical model ID off telemetry, which the resolver continues to emit.

Rust CLI: **no change** — does not reference model IDs.

Protos: **no change** — wire format unchanged.

## Test Strategy

- **Unit tests** (`tests/unit/python/test_model_aliases.py`):
  - Alias hit returns the configured `ResolvedModel`.
  - Raw vendor ID fallthrough returns `ResolvedModel(alias=None, raw=True)` and emits a deprecation warning exactly once per process.
  - Unknown alias **and** unknown raw prefix → `SystemExit` with clear message naming the offending string.
  - Provider mismatch (alias declares `anthropic` but env has only `OPENAI_API_KEY`) → existing startup warning fires, not a new failure mode.
  - Resolver is hot-pluggable via a test seam so individual tests can register a temporary alias map without disturbing the singleton.

- **Integration tests**:
  - Existing [`tests/unit/python/test_llm_client.py`](../../tests/unit/python/test_llm_client.py) exercises `create_provider` end-to-end; extend to assert the tuple shape and physical-model passthrough.
  - Existing planner / persona-runtime integration tests pass unchanged — proves the resolver is transparent at the call-site level.

- **Manual tests**:
  - Existing MT suite uses raw vendor IDs in some places (e.g., `MT-MEMORY-003` referenced `claude-haiku-4-5`). Phase 1 leaves these untouched (pass-through still works); Phase 2 sweeps them to aliases.

- **CI**:
  - Add a check that `config/agents.yaml` references only declared aliases (Phase 2+) — fails loudly on accidental raw-ID introduction.
  - The schema-version bump in [`config/optimization.yaml`](../../config/optimization.yaml) is validated by the existing config-loader test path.

## Open Questions

1. **Alias granularity — role vs capability?** This RFC proposes role-named aliases (`quality`, `fast`, `summarizer`). An alternative is capability tags (`tool_use_strong`, `long_context`). Roles are simpler and cover today's needs; capability tags can layer on top later if a use case demands them. **Tentative**: roles for v0.3.x; revisit when a multi-provider deployment surfaces a real "I need a model with capability X" decision the role names don't capture.

2. **Profile-scoped overrides?** [`config/optimization.yaml`](../../config/optimization.yaml) already has `active_profile: "default"` with profiles like `cost_optimized` / `speed_optimized`. Should each profile carry its own alias overrides (so the `quality` alias under `cost_optimized` resolves to Haiku, not Sonnet)? **Tentative**: yes, but the Phase 1 PR ships the base machinery only; profile overrides are a Phase 2 follow-up if dogfood shows they're needed.

3. **Per-tenant alias overrides for a future multi-tenant world?** Out of scope for this RFC; the alias map is process-global. If multi-tenancy lands (RFC TBD), this is the surface that would gain a tenant scope.

4. **Soft warning vs hard error on raw-ID usage during Phase 2.** This RFC proposes warning only. The cutover to hard error is Phase 3, gated by zero observed raw-ID usage — not by a calendar date.

5. **Operator override at run time?** Currently agents declare their model in YAML; there's no runtime override. Out of scope.

6. **Should Phase 1 include a CI lint that fails on new raw-ID introductions in `agents.yaml`?** Arguably yes — it prevents regression during the Phase 1 → Phase 2 window. **Tentative**: include in Phase 1.

7. **Should `active_profile` override per-agent aliases?** §J's tentative position is *no* — agent-declared aliases are authoritative; profiles only swap the routing-table defaults (the values referenced when an entry uses `task_agents` / `sub_agents` / `evaluators`). A persona that explicitly declares `model: quality` runs on `quality` regardless of whether the active profile is `cost_optimized` or `quality_optimized`. Revisit if dogfood shows operators want a per-profile downgrade knob (e.g., "save money on a side branch by demoting personas to `fast`").

## Decision / Next Steps

This RFC was accepted and its Phases 1–2 implemented in v0.3.4. The original proposal gated on:

1. Review of the alias-naming convention (Open Question 1) — resolved; `quality` / `fast` / `summarizer` shipped.
2. Confirmation that Phase 1 sits cleanly between in-flight RFC 0026 PRs without file conflicts — confirmed; the work landed without conflict.
3. ~~Author of a companion PR plan (`0033-pr-plan.md`) once accepted~~ — **done**: the companion [PR plan](0033-pr-plan.md) covers Phases 1–2 (the v0.3.4 contract), modeled on [`0024-pr-plan.md`](0024-pr-plan.md). It carries the [v0.3.4 provider-parity amendment](../v0.3.4-plan-amendment-2026-05-24.md)'s Phase 2 additions (missing-price guard, OpenAI peer alias + pricing, network-allowlist neutralization, local-pricing decision) alongside the RFC's original Phase 2.

The proximate motivator (Sonnet 4 retirement on 2026-06-15) provided a natural deadline for Phase 1 — but the *RFC itself* did not gate on that date. The migration was absorbed as a single `quality` alias edit (PR 1) plus pointing the agents at it (PR 3), so the deadline is met as a side effect of the abstraction rather than a one-off mechanical sweep.

### Implemented in v0.3.4 (Phases 1–2)

Phases 1–2 shipped under the v0.3.4 umbrella per [`0033-pr-plan.md`](0033-pr-plan.md) — PRs 1 ([#431](https://github.com/mkhomutov/Persatrix/pull/431)), 2 ([#432](https://github.com/mkhomutov/Persatrix/pull/432)), 3 ([#433](https://github.com/mkhomutov/Persatrix/pull/433)), 4 ([#434](https://github.com/mkhomutov/Persatrix/pull/434)), 5 ([#435](https://github.com/mkhomutov/Persatrix/pull/435)), 6 ([#436](https://github.com/mkhomutov/Persatrix/pull/436)). Every model reference now routes through the `models.aliases` map: `agents/model_aliases.py` `resolve()` maps a logical alias to a `(provider, model_id, pricing)` record; `create_provider` returns `(provider, physical_model)` so the alias name never reaches `create_message`; the Anthropic Sonnet 4 → 4.6 migration was absorbed by editing only the `quality` alias entry. No runtime path carries a literal vendor model ID (the `SubAgentRequest.model` default became `None`, resolved at construction per [§J](#j-persona-and-sub-agent-model-selection)). A missing-price guard fails closed for unpriced non-local aliases ([§E](#e-backwards-compatibility) raw-ID fall-through untouched); the legacy `cost.pricing.models` block is derived from the alias map and the `persatrix.llm.model_alias` span attribute is emitted, so the v0.3.2 cost surface reports correctly-keyed, non-zero cost across the re-keying. A priced OpenAI peer alias ships so the release is genuinely *Any Provider*. The raw-ID pass-through ([§E](#e-backwards-compatibility)) still works but fires a one-shot startup deprecation warning per agent and increments the `persatrix.llm.alias.raw_id_usage` counter.

**Still scheduled**: Phase 3 (remove the raw-ID fall-through, retire `_infer_provider` and the `provider_inference` block, schema bump to `"0.3"`, loader rejection of raw vendor IDs in `agents.yaml`) ships in v0.3.5+, **gated on observed traffic** — the `raw_id_usage` counter reading zero across the dogfood window, not a calendar date — per the [Phased Implementation Plan](#phased-implementation-plan). This is a partial-RFC closeout; the full-RFC closeout waits for Phase 3.

## Related Documentation

- [0033-pr-plan.md](0033-pr-plan.md) — companion PR plan (Phases 1–2; the v0.3.4 contract).
- [v0.3.4-plan.md](../v0.3.4-plan.md) — master plan this RFC's Phases 1–2 implement; [provider-parity amendment](../v0.3.4-plan-amendment-2026-05-24.md) the PR plan folds in.
- [Architecture Spec](../ai-agents-orchestration-spec.md) — references `claude-sonnet-4-20250514` in fallback-chain examples (Phase 2 doc sweep).
- [Extension Spec](../persatrix-extension-spec.md) — extensive use of literal vendor IDs (Phase 2 doc sweep).
- [Persona Agents Guide](../guides/persona-agents.md) — references both `claude-sonnet-4-20250514` and `claude-haiku-4-5` in pricing examples (Phase 2 doc sweep).
- [Anthropic Model Deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations) — vendor source of truth for retirement dates.
- [RFC 0004 — Python Agent gRPC Server](0004-python-agent-grpc-server.md) — original home of the `LLMProvider` Protocol that this RFC builds above.
- [RFC 0008 — Memory & Context Optimization](0008-agent-memory-context-optimization.md) — established `config/optimization.yaml` as the routing/cost config home.
- [RFC 0019 — OpenTelemetry Completion](0019-opentelemetry-completion.md) — Gen-AI span conventions and the `persatrix.*` reserved-namespace rule; the new `persatrix.llm.model_alias` attribute is additive within those conventions.
