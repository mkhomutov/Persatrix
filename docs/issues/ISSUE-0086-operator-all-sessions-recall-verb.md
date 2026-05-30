---
id: ISSUE-0086
summary: "RFC 0031 Phase 3 carved out `persatrix memory recall --all-sessions` — the only operator route to the `sessions=\"*\"` cross-session debug mode (RFC §D mode 3). Surfacing `\"*\"` to an operator means building an entire operator memory-inspection surface (a `persatrix memory` CLI verb + an orchestrator memory-recall REST endpoint + a gRPC recall path into each persona's `memory.db`), none of which exists today — a different story from the session *operator* surface Phase 3 shipped. Until it lands, the `\"*\"` sentinel keeps **no operator entry point at all**, so it provably cannot reach a prompt context — strictly stronger than the Phase 2 recall-filtering guarantee. Track the deferred verb (and reconfirm the security posture) here."
status: open
severity: low
area: cli
created: 2026-05-30
refs:
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0031-phase3-pr-plan.md
  - agents/memory/_principal_filter.py
---

## Summary

RFC 0031 Phase 3 ([Phase 3 PR plan](../rfcs/0031-phase3-pr-plan.md)) shipped the `persatrix session …` operator surface (registry verbs + active-session pointer + `--session` override). It **did not** ship `persatrix memory recall --all-sessions`, the verb the [v0.3.5 master plan §Phase 2 acceptance](../v0.3.5-plan.md) and [RFC §Security Considerations](../rfcs/0031-per-session-namespacing-channels.md#security-considerations) named as the only operator route to the `sessions="*"` cross-session debug recall (RFC §D mode 3). This issue carries that carve-out.

## Context

Planning research for Phase 3 found there is **no `persatrix memory` command, no memory-recall REST endpoint, and no recall RPC** anywhere today. The `sessions="*"` sentinel is library/test-only ([`agents/memory/_principal_filter.py`](../../agents/memory/_principal_filter.py) and the tier recall paths); it has no caller outside the unit/integration suites. Surfacing it to an operator is therefore not "add a flag" — it is building an entire **operator memory-inspection surface**:

- a `persatrix memory recall …` CLI verb (clap subcommand group; none exists);
- an orchestrator REST endpoint that reaches persona memory (none exists — the orchestrator never recalls *on behalf of* an operator);
- a gRPC recall path into each persona's `memory.db` (the personas expose no recall RPC; recall runs in-process during event handling).

That is a different story from the session operator surface — it is conspicuously absent from [RFC §E's own Phase 3 deliverable list](../rfcs/0031-per-session-namespacing-channels.md#e-operator-surface) — so Phase 3 carved it out (mirroring how Phase 4 carves out `persatrix memory legacy-prune`).

## Impact

- **No functional gap today.** Default recall is session-scoped (Phase 2, F-3 closed); the `legacy` carve-out keeps pre-RFC rows visible; operators create / switch / archive sessions (Phase 3). The only thing missing is a *debug* view that reads across sessions.
- **Security upside, deliberately kept.** With no operator entry point, the `"*"` sentinel **provably cannot reach a prompt context** — strictly stronger than the Phase 2 guarantee (which relied on no default path selecting `"*"`). Any future implementation must preserve this: `"*"` must remain an explicit, audited, operator-only out-of-band query, never a default recall path, and must compose with the unconditional `principal_id` filter (no all-principals sentinel — [RFC §D ISSUE-0081 amendment](../rfcs/0031-per-session-namespacing-channels.md#d-recall-semantics)).

## Proposed fix / investigation path

1. **Confirm the need.** Decide whether operator cross-session recall is actually wanted before building the surface; the dementia-test and isolation gates already exercise `"*"` at the library layer, so the verb is operator-convenience, not a correctness requirement.
2. **CLI verb** — a `persatrix memory recall <agent> [query] --all-sessions` subcommand group, thin-client over a new orchestrator endpoint (per the [rust-cli thin-client pattern](../../.github/instructions/rust-cli.instructions.md)).
3. **Orchestrator REST + persona recall RPC** — the orchestrator gains a memory-recall endpoint that fans out to the named persona over a new gRPC recall RPC into its `memory.db`. This is the bulk of the work and the reason for the carve-out.
4. **Security gate** — `--all-sessions` must be explicit, logged/audited, and still principal-bounded; never reachable from a default recall path or a prompt context. Add a TDD gate asserting `"*"` has no path into persona context except this verb.

> Maintainer owns whether this lands as a later RFC 0031 phase, a successor RFC (operator memory inspection), or stays carved out indefinitely. Filed at Phase 3 closeout so the deferral is tracked rather than implicit.
