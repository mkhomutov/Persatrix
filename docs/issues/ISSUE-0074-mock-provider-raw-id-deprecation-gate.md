---
id: ISSUE-0074
summary: "Per-agent `provider: mock` with a raw model id now trips the RFC 0033 raw-ID deprecation warning + Phase 3 `alias.raw_id_usage` gate counter (mock skipped resolve() pre-v0.3.4). Phase 3 open question: count mock agents or exempt them? Latent — no shipped config triggers it."
status: resolved
severity: low
area: agents/llm_factory
created: 2026-05-27
closed: 2026-06-01
refs:
  - docs/rfcs/0033-model-alias-layer.md
  - docs/v0.3.4-plan-amendment-2026-05-27.md
  - agents/llm_factory.py
  - agents/model_aliases.py
---

> **Resolved 2026-06-01** — decided by **RFC 0033 Phase 3** (shipped in v0.3.5
> as the conditional co-resident, [#481](https://github.com/mkhomutov/Persatrix/pull/481)
> / [#482](https://github.com/mkhomutov/Persatrix/pull/482)), which makes the
> open question moot: Phase 3 retired the §E raw-vendor-ID pass-through
> entirely, so there is no longer a raw-ID path, no `_note_raw_id_usage` call,
> and no `persatrix.llm.alias.raw_id_usage` counter to nudge. This is **candidate
> disposition 2** (mock agents reference a declared alias) made structural: a
> per-agent `provider: mock` with a *raw* model id no longer warns-and-counts —
> the resolver rejects it up front with a loud `SystemExit` naming the string
> ([agents/model_aliases.py](../../agents/model_aliases.py) `resolve`), exactly
> as for any other provider. Disposition 1 (exempt local providers) is therefore
> unnecessary; there is nothing left to exempt.
>
> - [tests/unit/python/test_llm_offline.py](../../tests/unit/python/test_llm_offline.py)
>   `test_create_provider_per_agent_mock_raw_model_id_is_rejected` pins the
>   contract executably: a `provider: mock` agent naming a raw vendor id hits
>   `SystemExit` (`not a declared alias`) at resolve, before the per-agent
>   provider field is consulted — so a future raw-id-for-mock escape hatch trips
>   this test.
> - [docs/guides/model-providers.md](../guides/model-providers.md) — the
>   single-agent provider opt-in recipe was **stale** (it told operators to set
>   `provider: ollama`, `model: llama3.2` directly, a raw tag that Phase 3 now
>   rejects). Corrected to the alias-based recipe: declare a per-agent alias and
>   point the agent's `model:` at it.

## Summary

The v0.3.4 knob-free provider-parity refactor (PR #440) folded the `mock`
provider into the standard `create_provider` dispatch. As a side effect, a
**per-agent `provider: mock` with a raw vendor model id** now trips the RFC 0033
raw-ID deprecation path — a one-shot deprecation warning **and** an increment of
the per-agent `persatrix.llm.alias.raw_id_usage` counter, which is the RFC 0033
Phase 3 entrance-gate signal (must read zero to authorise retiring the raw-ID
pass-through + `_infer_provider`). Pre-refactor, `mock` short-circuited *before*
`resolve()`, so it never reached that accounting.

This is latent today — no shipped config triggers it (stock agents reference
aliases; the `make demo-*` overlays route `mock` / `ollama` via **alias**
entries, so they resolve `raw=False`). It surfaces only for a hand-rolled
single-agent mock opt-in.

## Detail

`agents/llm_factory.py::create_provider`:

```python
resolved = resolve_model(model, explicit_provider=explicit_provider)
if resolved.raw:
    _note_raw_id_usage(str(agent_id), model)   # warning + raw_id_usage counter
```

For `{provider: mock, model: "claude-sonnet-4-6"}` (a raw id, not a declared
`models.aliases` entry), `resolve()` takes the raw path (the explicit provider
wins) and returns `raw=True`, so `_note_raw_id_usage` fires.

Why the documented per-agent mock opt-in realistically hits this: referencing a
declared alias name *and* setting `provider: mock` is a §D-rule-1 mismatch
(`SystemExit`) whenever that alias resolves to a non-mock provider — so to mock a
single agent you must give it a raw model id (or point it at a mock-provider
alias). With a raw id, the agent now (a) gets a deprecation warning advising it
to "migrate to an alias so a vendor retirement / provider swap is a one-line
edit" — misleading, since `MockProvider` ignores the model entirely — and
(b) nudges the Phase 3 gate counter off zero.

## Open question / candidate dispositions

Whether this is a regression or an *improvement* depends on Phase 3 (RFC 0033
§I) semantics:

- If Phase 3 will require **every** agent model — mock agents included — to be a
  declared alias, then counting the raw-model mock agent is **correct** (it
  genuinely relies on the raw-ID pass-through). Current behaviour is then right;
  only the warning copy is arguably noisy for a mock.
- If mock agents are meant to be **exempt** (their `model` is a $0 placeholder
  the provider ignores), the counting + warning is noise.

Candidate fixes, for the Phase 3 decision-maker:

1. **Exempt local providers** (at least `mock`) from `_note_raw_id_usage` in
   `create_provider`; pin "a per-agent `provider: mock` emits no RFC 0033
   deprecation warning and does not increment `raw_id_usage`" with a test.
2. **Keep current behaviour** and document the canonical single-agent mock as a
   `provider: mock` **alias** (so it resolves `raw=False`), making the warning a
   correct nudge.
3. **No action** if the niche is judged immaterial before Phase 3.

No production config is affected today, so no change is proposed for the v0.3.4
release; this is a Phase 3 input. Surfaced by the PR #440 deep review.
