---
id: ISSUE-0071
summary: "The RFC 0033 PR 4 missing-price guard fires per-resolve (scoped to the resolved alias), so an unpriced non-local alias that no agent resolves is never caught at runtime: an all-local/offline society (whose agents take the factory's mock/ollama/offline early-return before resolve()) boots without validating any cloud alias, and validate_alias_pricing() — the whole-map validator that would close the gap — is wired only into tests, not an eager startup check"
status: resolved
severity: low
area: agents/optimization
created: 2026-05-26
closed: 2026-05-31
refs:
  - docs/rfcs/0033-model-alias-layer.md
  - docs/rfcs/0033-pr-plan.md
  - docs/v0.3.4-plan-amendment-2026-05-24.md
---

> **Resolved 2026-05-31** — adopted **proposed fix option 1**: `validate_alias_pricing()`
> (no-arg, whole-map) is now wired into the server bootstrap via a new
> `agents.server_cli._validate_startup_config()` seam, called from `main()`
> after logging is configured and **before the socket is bound**. A
> misconfigured registry — including an *unused* unpriced non-local alias, and
> in an all-local/offline society that never resolves a cloud alias — now fails
> fast and loud at boot, naming the offending alias, rather than only on first
> resolve (or never). The per-resolve scoped guard stays as the runtime
> backstop; local ($0-real) aliases remain exempt. Covered by
> `tests/unit/python/test_server_cli_startup_validation.py`.

## Summary

The missing-price guard (RFC 0033 PR 4, [#434](https://github.com/mkhomutov/Persatrix/pull/434))
fails closed **per-resolve, scoped to the resolved alias** — the correct
runtime safety property (no agent ever runs with a silently-$0 budget gate; see
the review fix in `test_unrelated_unpriced_alias_does_not_break_a_good_resolve`).
It does **not** provide an eager *whole-config* check at startup. An unpriced
non-local alias that no agent ever resolves is therefore never caught at
runtime, and `validate_alias_pricing()` — the whole-map validator that exists
expressly for an explicit startup / CI sweep — is wired only into tests.

## Context

Found during the #434 deep review (the per-resolve scoping fix landed in the
same review).

- [agents/model_aliases.py](../../agents/model_aliases.py) — `resolve()` runs
  `_check_entry_pricing(alias, entry)` on the resolved config-backed entry only;
  `validate_alias_pricing()` / `_check_alias_pricing()` iterate the whole map
  but have no runtime caller outside tests.
- [agents/llm_factory.py:137-168](../../agents/llm_factory.py) — the
  `offline_mode_enabled()` / `provider == "mock"` and `ollama_mode_enabled()`
  branches **return before** the `resolve_model(...)` call (line 181). A society
  whose agents all take one of these early-return paths never resolves a
  config-backed alias, so the guard never fires there.
- The shipped `config/optimization.yaml` is fully priced and every stock agent
  resolves a priced/local alias, so a normal cloud deployment **does** fail
  closed at first agent creation. The gap is narrow: an *unused* misconfigured
  cloud alias, or an all-local/offline deployment.
- The JSON schema ([schemas/optimization.schema.json](../../schemas/optimization.schema.json))
  marks `input_per_1m_tokens` / `output_per_1m_tokens` **required** on every
  alias entry, and `make validate` ([agents/validate.py](../../agents/validate.py))
  enforces it — so an unpriced alias (used or not, local or not) already fails
  the static `make validate` / CI sweep *before it can ship*. The gap below is
  therefore **runtime-only**: a config that bypassed that sweep (a hand-edited
  deployment, a dev checkout that never ran `make validate`) whose bad alias is
  then never resolved.

## Impact

Low, and narrower than "no startup check" suggests — the static layer already
covers the pre-ship case (see Context: `make validate` rejects an unpriced
alias). The runtime guard's safety property holds for every alias actually used
(it fails closed before that alias's first LLM call). The residue is a
runtime-only config-hygiene gap, reachable only when `make validate` was
skipped:

1. **An unused misconfigured alias is not surfaced at *runtime* boot.** It is
   caught by `make validate` / CI; what is missing is a runtime signal if that
   check was skipped *and* nothing ever resolves the alias.
2. **All-local/offline societies never validate cloud aliases at runtime.** No
   live budget hole (nothing non-local runs), and `make validate` still flags
   the latent config error statically.

This is the gap the PR docstrings originally over-claimed as "fail closed at
startup" (corrected to "per-resolve, scoped to the resolved alias" in #434).

## Proposed fix / investigation path

Decide whether to add a deliberate, loud whole-config check at process startup:

1. **Wire `validate_alias_pricing()` (no-arg) into server bootstrap**, once, so
   a misconfigured *any* alias fails the boot loudly regardless of which
   provider mode is active. Smallest change; restores the stronger
   "whole-config at startup" guarantee as an explicit, intentional check rather
   than an emergent side effect of the first resolve.
2. **Fold a runtime check into the optimization-config loader** so a
   misconfigured alias fails *boot* loudly, not only `make validate`. NB the
   *static* half of this already exists — the schema marks pricing required and
   `make validate` enforces it (see Context) — so what is genuinely missing is
   the *runtime* (loader / bootstrap) enforcement, which only matters for a
   config that skipped the static sweep.

Weigh against the policy question: should an *unused* misconfigured alias block
boot at all? Option 1 says yes (strict hygiene); the current per-resolve
behaviour says no (fail only on use). Given `make validate` already blocks it
pre-ship, the marginal value of either runtime option is the bypassed-CI case
only — pick one and document it, or close as won't-fix if the static sweep is
deemed sufficient.

## Notes

> 2026-05-26 — captured during the #434 deep review, alongside the per-resolve
> scoping fix. Not a #434 blocker: the runtime safety property is delivered by
> the scoped guard; this is the whole-config-hygiene follow-up.
