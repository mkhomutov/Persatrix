---
id: ISSUE-0051
summary: "Per-session memory namespacing for channels + persona memory — F-3 root-cause fix (currently mitigated by `make reset` operator workaround)"
status: open
severity: medium
area: agents/memory
created: 2026-05-12
refs:
  - docs/v0.3.0-test-findings-pr-plan.md
  - docs/guides/channels.md
  - docs/guides/persona-agents.md
---

## Summary

Cross-run state bleed (F-3 in [v0.3.0 channel test findings](../v0.3.0-test-findings-pr-plan.md))
is currently mitigated by a `make reset` operator-workaround target that
purges the Docker named volumes between runs. The root-cause fix —
per-session namespacing so reruns with the same channel name + user id
are auto-isolated — is deferred to this issue.

## Context

Persona memory (`ember-owl-data` volume → `/app/data/memory.db`) and the
orchestrator channels store (`orchestrator-data` volume →
`/var/lib/persatrix/channels.db`) persist across stack restarts. A
second test run with the same channel name and same `--user` identity
inherits prior content unless the volumes are explicitly purged. The
personas surface old participants and topics from prior runs, which
steers the next conversation off-topic within ~2 turns.

PR 6 of the v0.3.0 channel test-findings plan
([fix/v030-channel-state-reset](../v0.3.0-test-findings-pr-plan.md#pr-6-fixv030-channel-state-reset--state-reset-make-target))
adds `make reset` (`docker compose down -v` + confirmation) and
operator-guide subsections in [channels.md](../guides/channels.md) and
[persona-agents.md](../guides/persona-agents.md). That is **operator
ergonomics**, not the actual F-3 fix.

## Impact

- Manual test reruns require an explicit `make reset` to be reliable;
  forgetting it produces off-topic persona behaviour that looks like a
  regression but is actually prior-run carryover.
- CI / automated regression harnesses that share volumes across runs
  cannot be authored cleanly without per-session isolation primitives.
- Channel-id collisions across distinct test scenarios (same `planning`
  name reused) re-attach prior history rather than starting fresh.

## Proposed fix / investigation path

Per-session memory namespacing — sketch, not a commitment:

- A `session_id` (or test-scope id) at channel-creation time, scoping
  the row keys in `channels.db` and the tag/scope filters in
  `agents/memory/episodic.py` + `agents/memory/relationship.py`.
- A `scope` column already exists on episodes (`episodes.scope`, added
  by RFC 0020 PR migration v5; see `agents/memory/episodic.py` ~L190
  and the recall filter in `agents/memory/scope_recall.py`). Open
  question: can it be widened to carry a session id without colliding
  with the interaction-lifecycle semantics RFC 0020 §G assigns to
  scope (per-interaction boundary, not per-test-run), or is a separate
  dimension cleaner? Resolve before reusing the column.
- Decide whether scoping is opt-in (test harness sets it) or implicit
  (every channel is session-scoped). Implicit changes the production
  recall surface and needs an RFC-level discussion.

Open questions:

- Does this overlap with RFC 0008 / 0020 memory-scoping work already in
  flight? Check before introducing a new scoping dimension.
- Is the namespacing operator-visible (e.g. `persatrix session new`)
  or purely an internal harness primitive?

## Notes

> 2026-05-12 — captured during PR 6 of the v0.3.0 channel test-findings
> plan. `make reset` is in place as the operator workaround; this
> issue tracks the root-cause fix.
