---
id: ISSUE-0072
summary: "The three memory-compression LLM surfaces still hardcode raw vendor model IDs as parameter defaults (claude-haiku-4 / claude-haiku-4-5) instead of routing through the RFC 0033 alias layer — the last model-identity literals in production Python, and stale (the canonical Haiku id is claude-haiku-4-5-20251001), so a future cost-path migration would price them at $0."
status: open
severity: low
area: memory
created: 2026-05-26
refs:
  - docs/rfcs/0033-model-alias-layer.md
  - docs/rfcs/0033-pr-plan.md
  - docs/issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md
---

## Summary

RFC 0033 made `config/optimization.yaml`'s `models.aliases` block the single
source of truth for model identity, and PR 3 ("drop last model literal") flipped
`config/agents.yaml`, the routing defaults, the summarisation reference, and
`agents/persona_types.py` to alias references. Three LLM call sites were missed:
the memory-compression surfaces still carry **hardcoded raw vendor model IDs as
parameter defaults**.

- `agents/memory/working.py:63` — `compression_model: str = "claude-haiku-4-5"`
- `agents/memory/episodic_retention.py:45` — `compression_model: str = "claude-haiku-4"`
- `agents/memory/episodic.py:413` — `compression_model: str = "claude-haiku-4"`

These are the only model-identity string literals left in non-test production
Python (`agents/llm_ollama.py`'s `DEFAULT_OLLAMA_MODEL` and the provider-prefix
tuples in `llm_client.py` / `model_aliases.py` are provider-inference patterns,
not alias-routed model identity).

## Context

Found during the PR #435 (RFC 0033 PR 5) review while confirming which
`create_message` call sites thread the new `model_alias` span attribute. PR 5
threads it for `base.py`, `action_loop.py`, and `summarize_close.py`; the two
memory-compression call sites (`working.py:183`, `episodic_retention.py:110`) do
not — correctly, because they are on the §E raw-ID pass-through path, so there is
no alias to emit. That raw-ID path is exactly the problem: the model identity is
code-baked rather than config-owned.

- `agents/persona.py:306` instantiates `WorkingMemory(max_tokens=...)` with **no**
  `compression_model` argument, so the `"claude-haiku-4-5"` default is reachable
  in production, not just a test affordance.
- The defaults are **stale**: the shipped alias map prices
  `claude-haiku-4-5-20251001` (the `fast` / `summarizer` physical model). Neither
  `"claude-haiku-4-5"` nor `"claude-haiku-4"` matches that — or any — key in the
  RFC 0033 §F derived `cost.pricing.models` table.

This contradicts the RFC 0033 / `agents/optimization.py` module-docstring
principle that "model identity lives only in config … a code-baked default would
silently re-route to a model the operator never chose."

## Impact

**Today: latent, not active.** Per
[ISSUE-0063](ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md), the
memory-compression calls pass no `cause`, so they are un-leased and their spend
reaches neither a wallet lease nor the budget `TokenCounter` — it is *uncounted*,
not mis-counted. So `EstimateCost` is never actually invoked for these model IDs
yet, and the $0 mismatch does not bite in the current shipped surface.

**When those surfaces migrate to the leased / counted cost path** (the
un-migrated v0.2.3 origins RFC 0023 PRs 4–6 wire — see the `create_message`
docstring), the stale ID flows into `internal/cost/config.go::EstimateCost`,
misses the pricing table (`config.go:142` — model-name mismatch → `return 0`,
logged at Debug), and the call's spend is silently priced at $0. That is the same
class of cost-attribution hole RFC 0033 PR 5 just closed for the `quality` path
(the PR 3 cost-regression row), still latent here.

Independently of cost, this is a model-identity governance gap: a Haiku
retirement or provider swap requires editing these three literals by hand, the
exact sweep the alias layer was built to eliminate.

## Proposed fix / investigation path

1. Route the three surfaces through an alias rather than a raw default. The
   natural target is the `summarizer` alias (memory compression is summarisation)
   or `fast`; resolve it via `agents/model_aliases.py` `resolve()` the same way
   `agents/persona_runtime/summarize_close.py` does, and thread the resolved
   `.alias` as `model_alias=` into `create_message` so the §G span attribute is
   emitted once these calls are alias-routed.
2. Decide where the default comes from. To honour "no hardcoded model defaults,"
   the `compression_model` parameter should default to a config-derived alias
   (e.g. via a small accessor over `models.aliases`) rather than a literal — or
   the caller (`agents/persona.py`) should resolve and pass it explicitly.
3. Confirm reachability of `episodic.py:413` / `episodic_retention.py:45` (the
   episode-retention path) the way `working.py` was confirmed via `persona.py`.
4. Tests: assert the compression surfaces resolve through the alias map (not a
   literal), and `cd agents && mypy .` clean.

This is a candidate fold-in for the RFC 0023 PRs 4–6 cost-path migration of these
origins, or a standalone RFC 0033 follow-up — whichever lands the leasing of the
memory surfaces first, since the re-keying only matters once these calls are
counted.

## Notes

> 2026-05-26 — initial capture during PR #435 (RFC 0033 PR 5) review. Distinct
> from ISSUE-0063 (which covers the *un-leased / uncounted* aspect of the same
> `working.py:183` call): this issue is the *hardcoded, stale model identity*,
> which bites independently when the surface is eventually counted.
