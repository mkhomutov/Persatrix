---
id: ISSUE-0070
summary: "SubAgentRequest.__post_init__ resolves the model default via sub_agent_default_model(), which raises SystemExit (a BaseException) when config lacks the sub_agents routing default — latent until SPAWN_SUB_AGENT is wired, at which point the spawn site must catch it (as summarize_close.py does) or a misconfig escapes `except Exception` and tears down the loop"
status: open
severity: low
area: agents/persona
created: 2026-05-26
refs:
  - docs/rfcs/0033-model-alias-layer.md
  - docs/rfcs/0033-pr-plan.md
---

## Summary

RFC 0033 PR 3 ([#433](https://github.com/mkhomutov/Persatrix/pull/433)) moved
`SubAgentRequest`'s model default into `__post_init__`, which calls
`sub_agent_default_model()`. When the optimization config declares no
`default.model_routing.defaults.sub_agents`, that accessor raises `SystemExit`
([agents/optimization.py:199](../../agents/optimization.py)). `SystemExit` is a
`BaseException` — it is **not** caught by `except Exception`. So constructing
this dataclass is now a config-read + fail-loud side effect, not a pure
in-memory operation.

## Context

Found during the #433 review.

- [agents/persona_types.py:130-143](../../agents/persona_types.py) —
  `__post_init__` resolves `model is None` → `sub_agent_default_model()`.
- [agents/optimization.py:180-205](../../agents/optimization.py) —
  `sub_agent_default_model()` raises `SystemExit` naming the missing key when
  `sub_agents` is absent (deliberate: no hardcoded fallback, RFC 0033).
- **Latent today.** `SPAWN_SUB_AGENT` is a not-implemented stub
  ([agents/action_executor.py:195-214](../../agents/action_executor.py)), so no
  live runtime path builds a `SubAgentRequest` from an LLM action — only tests
  and the `spawn_sub_agent` forwarding stub do. The shipped config always
  carries `sub_agents: quality`, so the `SystemExit` never fires in practice.
- **Precedent for correct handling.**
  [agents/persona_runtime/summarize_close.py:163-173](../../agents/persona_runtime/summarize_close.py)
  deliberately wraps `resolve()` (also `SystemExit`-raising) in
  `except SystemExit` and degrades to the deterministic fallback, with a comment
  noting the per-call surface must not let a `BaseException` escape as an
  uncaught task exception.

## Impact

Low / latent. Two concerns, both surfacing only once `SPAWN_SUB_AGENT` is wired:

1. **Failure-mode placement.** When the spawn path goes live, a misconfig (no
   `sub_agents` default) raises `SystemExit` at the moment an agent decides to
   spawn a sub-agent — deep inside the persona/orchestrator event loop. Because
   `SystemExit` bypasses `except Exception`, a spawn site that does not catch it
   (the way `summarize_close.py` does) can let it propagate and tear down the
   loop rather than failing the single action.
2. **Pure-data type coupled to global config.** Construction now reads the
   process-wide optimization `lru_cache`, so a previously-pure dataclass depends
   on config + cache state. E.g.
   [tests/unit/python/test_persona_agent_validation.py:121](../../tests/unit/python/test_persona_agent_validation.py)
   constructs `SubAgentRequest(role=..., task=...)` and now implicitly relies on
   the shipped config and an unpolluted cache. Safe today (the #433 fixtures
   reset the cache on teardown), but it is latent test-ordering fragility.

## Proposed fix / investigation path

Decide when wiring `SPAWN_SUB_AGENT`:

1. **Catch at the spawn site.** Mirror `summarize_close.py` — wrap
   `SubAgentRequest` construction / the spawn handler in `except SystemExit` and
   surface a `SUB_AGENT` failure result rather than letting it escape. Minimal;
   keeps construction-time resolution (RFC 0033 §J.3).
2. **Move resolution out of `__post_init__`** to the spawn site, where there is
   already an async context and error handling, so the data type stays pure and
   the fail-loud lands where it can be handled. Larger; revisit §J.3's
   "construction-time" wording first.

## Notes

> 2026-05-26 — initial capture during PR #433 review. Not a #433 blocker: the
> path is latent (`SPAWN_SUB_AGENT` stubbed) and the shipped config always
> declares `sub_agents`. The decision belongs with the PR that wires
> `SPAWN_SUB_AGENT`.
