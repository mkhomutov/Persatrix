---
id: ISSUE-0086
summary: "RFC 0031 Phase 3 carved out `persatrix memory recall --all-sessions` — the only operator route to the `sessions=\"*\"` cross-session debug mode (RFC §D mode 3). Surfacing `\"*\"` to an operator means building an entire operator memory-inspection surface (a `persatrix memory` CLI verb + an orchestrator memory-recall REST endpoint + a gRPC recall path into each persona's `memory.db`), none of which exists today — a different story from the session *operator* surface Phase 3 shipped. Until it lands, the `\"*\"` sentinel keeps **no operator entry point at all**; since RFC 0049 PR 4 (#784) the persona's facts recall does pass it, behind the RFC 0037 §D gate (F-3 is now \"no ungated widening\"). Track the deferred verb (and reconfirm the security posture) here."
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

Planning research for Phase 3 found there is **no `persatrix memory` command, no memory-recall REST endpoint, and no recall RPC** anywhere today. The `sessions="*"` sentinel has no operator caller. When this was filed it had no caller outside the unit/integration suites; since RFC 0049 PR 4 ([#784](https://github.com/mkhomutov/Persatrix/pull/784)) the persona runtime passes it too, and [`test_cross_session_read_sites.py`](../../tests/unit/python/test_cross_session_read_sites.py) lists every caller and what guards it. Surfacing it to an operator is therefore not "add a flag" — it is building an entire **operator memory-inspection surface**:

- a `persatrix memory recall …` CLI verb (clap subcommand group; none exists);
- an orchestrator REST endpoint that reaches persona memory (none exists — the orchestrator never recalls *on behalf of* an operator);
- a gRPC recall path into each persona's `memory.db` (the personas expose no recall RPC; recall runs in-process during event handling).

That is a different story from the session operator surface — it is conspicuously absent from [RFC §E's own Phase 3 deliverable list](../rfcs/0031-per-session-namespacing-channels.md#e-operator-surface) — so Phase 3 carved it out (mirroring how Phase 4 carves out `persatrix memory legacy-prune`).

## Impact

- **No functional gap today.** Default recall is session-scoped (Phase 2, F-3 closed) except the facts and episodic tiers, which read across sessions behind the RFC 0037 §D gate since RFC 0049 PR 4; the `legacy` carve-out keeps pre-RFC rows visible; operators create / switch / archive sessions (Phase 3). The only thing missing is a *debug* view that reads across sessions.
- **Security posture.** No operator can pass `"*"`. The persona runtime can: its facts recall passes it and every fact it returns goes through the RFC 0037 §D gate before the prompt, so F-3 is now "no *ungated* widening" and the other persona recall tiers stay session-scoped. Any future implementation must preserve this: the verb must be an explicit, audited, operator-only out-of-band query, never an ungated path into a prompt, and must compose with the unconditional `principal_id` filter (no all-principals sentinel — [RFC §D ISSUE-0081 amendment](../rfcs/0031-per-session-namespacing-channels.md#d-recall-semantics)).

## Proposed fix / investigation path

1. **Confirm the need.** Decide whether operator cross-session recall is actually wanted before building the surface; the dementia-test and isolation gates already exercise `"*"` at the library layer, so the verb is operator-convenience, not a correctness requirement.
2. **CLI verb** — a `persatrix memory recall <agent> [query] --all-sessions` subcommand group, thin-client over a new orchestrator endpoint (per the [rust-cli thin-client pattern](../../.github/instructions/rust-cli.instructions.md)).
3. **Orchestrator REST + persona recall RPC** — the orchestrator gains a memory-recall endpoint that fans out to the named persona over a new gRPC recall RPC into its `memory.db`. This is the bulk of the work and the reason for the carve-out.
4. **Security gate** — `--all-sessions` must be explicit, logged/audited, and still principal-bounded; never reachable from a prompt context. Add the verb's read to `CROSS_SESSION_READ_SITES` in [`test_cross_session_read_sites.py`](../../tests/unit/python/test_cross_session_read_sites.py), which fails on any cross-session read it does not list.

> Maintainer owns whether this lands as a later RFC 0031 phase, a successor RFC (operator memory inspection), or stays carved out indefinitely. Filed at Phase 3 closeout so the deferral is tracked rather than implicit.
