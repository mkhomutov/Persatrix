---
id: ISSUE-0127
summary: "`FactStore.delete_by_subject` is now principal-scoped (ISSUE-0081 residual, v0.3.14 PR 3), and the principal predicate has no `\"*\"` sentinel by design — so there is no way to erase a subject across tenants. That is the right default for a *person's* own erasure request, but RFC 0013's `SubjectErasure` (target v0.5.0) is an **operator**-initiated traversal with no principal of its own: run as-is it would silently erase only whichever tenant the calling process resolves, and report that partial count as the audited `records_deleted`. The caller audit found no in-tree caller today, so nothing is broken now — but RFC 0013 cannot ship until it decides whether the right to be forgotten spans tenants, and what verb expresses it. Write-side sibling of ISSUE-0086."
status: open
severity: low
area: memory
created: 2026-08-11
refs:
  - agents/memory/_facts_erasure.py
  - agents/memory/_principal_filter.py
  - agents/memory/facts.py
  - docs/rfcs/0013-legal-ethical-compliance.md
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md
  - docs/issues/ISSUE-0086-operator-all-sessions-recall-verb.md
  - tests/unit/python/test_facts_erasure_principal_scope.py
---

## Summary

Erasure can no longer cross tenants — and the one caller that will ever
need to is an operator traversal that has no tenant.

## Context

v0.3.14 PR 3 closed the ISSUE-0081 erasure residual: both DELETEs in
[`_facts_erasure.delete_by_subject`](../../agents/memory/_facts_erasure.py)
now carry `AND principal_id = ?`, resolved through the same
[`resolve_active_principal`](../../agents/memory/_principal_filter.py) seam
the recall and write paths use. A principal can erase exactly the rows it
could read — the write-side mirror of the strict recall predicate, and the
fix for the boundary that would otherwise have broken on the day
[ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
Part 2 made principals real.

The principal predicate deliberately has **no** `"*"` sentinel and no
carve-out ([RFC 0031 §C/§D amendment](../rfcs/0031-per-session-namespacing-channels.md);
the ISSUE-0081 strict-isolation decision). So "erase this subject
everywhere" is now unspellable through the primitive.

The caller audit run with the fix found **no in-tree caller at all**: the
primitive has been a shipped-early storage primitive since RFC 0026 PR 1,
waiting on [RFC 0013](../rfcs/0013-legal-ethical-compliance.md)'s
`SubjectErasure` umbrella (target v0.5.0). Its only callers today are the
unit suites, which drive it under an explicit `principal_scope`. Nothing
is broken now — this issue exists so RFC 0013 does not discover the
question at implementation time.

## Impact

Bounded to the future RFC 0013 caller, but sharp when it lands:

- **A GDPR traversal that silently under-deletes.** `SubjectErasure` is
  operator-initiated — it runs in a process with no authenticated
  principal, so `resolve_active_principal` returns the construction
  snapshot (`'local'` in every default deployment). Run against a
  multi-tenant store it would erase only the `'local'` rows and leave
  every authenticated person's facts about the subject in place.
- **…and mis-reports it.** The return value is the map RFC 0013 audits as
  `records_deleted`. A partial traversal reporting a confident count is
  worse than a failure: the audit record says the obligation was
  discharged.
- **The tension is real, not a bug to patch away.** A cross-principal
  erasure verb is, mechanically, the cross-tenant write primitive the
  boundary forbids. It cannot be a default path or an ambient fallback;
  if it exists it must be explicit, audited, operator-only, and
  out-of-band — the same posture [ISSUE-0086](ISSUE-0086-operator-all-sessions-recall-verb.md)
  reserves for the read-side `sessions="*"` sentinel.

## Proposed fix / investigation path

1. **Decide the policy first — it is RFC 0013's, not a predicate.** Does
   the right to be forgotten span tenants? Two defensible answers: a
   subject's erasure is *per-account* (each person erases their own
   record of the subject; the operator has no cross-tenant verb), or it
   is *global* (a legal obligation attaches to the deployment, and the
   operator must be able to discharge it in one call). The RFC 0031 §C
   review note already flagged this as an RFC 0013 decision.
2. **If per-account** — no code change. Record the decision in RFC 0013
   §C and have `SubjectErasure` require an explicit principal argument
   rather than resolving one ambiently, so a caller cannot get a partial
   traversal by omission. Close this issue on that note.
3. **If global** — add an explicit all-principals erasure entry point
   (e.g. an `all_principals: bool` keyword that omits the predicate, or a
   separate `delete_by_subject_all_principals` verb) that: is never
   reachable from a per-request path, emits an audit record naming the
   operator, and returns per-principal subtotals so the audited count is
   honest about what it crossed. Gate it with the mirror of this
   release's test — *a per-request caller cannot reach the verb* — in
   [`test_facts_erasure_principal_scope.py`](../../tests/unit/python/test_facts_erasure_principal_scope.py).
4. **Either way, extend the audit.** `delete_by_subject` is silent at the
   storage layer by design (RFC 0026 §G; pinned by
   `test_fact_store_audit.py`); the umbrella caller owns the audit
   record, and that record must name the principal scope the traversal
   ran under.

## Notes

Filed at the v0.3.14 PR 3 caller audit, which the
[v0.3.14 plan](../v0.3.14-plan.md#pr-3--featurev0314-issue0081-erasure-principal-scope)
scopes as a PR deliverable: *"audit the callers so an operator-initiated
erasure has a defined principal — if that needs an all-principals verb it
files as an issue and slots v0.4.0."* Slotted **v0.5.0** rather than
v0.4.0: the plan's slotting assumed the verb would be needed by a caller
that exists, and the audit found none — the deciding caller is RFC 0013's
`SubjectErasure`, whose own target is v0.5.0. Nothing between now and then
can reach the gap, and deciding the policy earlier than the RFC that owns
it would be deciding it twice.

The agent-global **capacity** sweeps (episode TTL + size-cap eviction,
procedural decay, superseded-fact prune, note prune) are a different
class and are *not* covered here — they stay agent-global as a named
Known Gap scoped to v0.4.0 per the ISSUE-0081 plan-opening split
(capacity policy, not read-confidentiality).
