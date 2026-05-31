---
id: ISSUE-0051
summary: "Per-session memory namespacing for channels + persona memory — F-3 root-cause fix (currently mitigated by `make reset` operator workaround)"
status: resolved
severity: medium
area: agents/memory
created: 2026-05-12
closed: 2026-05-30
closed_pr: 471
refs:
  - docs/v0.3.0-test-findings-pr-plan.md
  - docs/guides/channels.md
  - docs/guides/persona-agents.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
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
>
> 2026-05-12 — design proposed as [RFC 0031 — Per-Session Namespacing for
> Channels and Persona Memory](../rfcs/0031-per-session-namespacing-channels.md).
> The RFC bakes in the implicit-scoping + operator-visible CLI direction
> and surfaces the dementia-test continuity tension (RFC OQ 1) as the
> load-bearing prerequisite before any implementation PR opens. This
> issue stays `open` until the RFC reaches `✅ Implemented`.
>
> 2026-05-29 — **F-3 root cause closed by RFC 0031 Phase 2 (v0.3.5).**
> Default recall is now session-scoped across all four persona-memory
> tiers (`episodes` / `relationships` v7, `facts` v8, `notes` v9) with
> the always-visible `'legacy'` carve-out, so a rerun reusing the same
> channel name + `--user` under a new session no longer inherits the
> prior run's memory ([Phase 2 PR plan](../rfcs/0031-phase2-pr-plan.md),
> PRs 1–6). The multi-persona-process correctness gaps that recall
> filtering exposed were closed in the same release
> ([ISSUE-0081](ISSUE-0081-session-id-process-global-not-task-local.md)
> task-local session id +
> [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
> Part 1 per-request `persatrix-session` emission). This issue stays
> `open` until the Phase 4 operator-docs closeout (`docs/guides/sessions.md`
> + `make reset` deprecation breadcrumb), where it is closed.
>
> 2026-05-30 — **Closed at the RFC 0031 Phase 4 docs closeout (v0.3.5).**
> [`docs/guides/sessions.md`](../guides/sessions.md) ships the operator
> guide (resolution chain, `legacy` carve-out, the split-volume `make
> reset` asymmetry, the no-secrets-in-labels note); the
> [channels.md](../guides/channels.md) / [persona-agents.md](../guides/persona-agents.md)
> `make reset` subsections carry the reframed breadcrumb; and
> [RFC 0031](../rfcs/0031-per-session-namespacing-channels.md) reaches
> `✅ Implemented` (all four phases shipped). The F-3 **recall** root cause
> is closed: default recall is session-scoped, so a run under a fresh
> session id inherits no prior run's memory.
>
> **Carried forward (not regressed):** the scope-axes reframing
> ([memory-scope-axes.md](../memory-scope-axes.md)) redefines `session` as
> room-continuity `(agent, channel)` that *accumulates* — so a rerun
> reusing the same channel name now auto-binds to the *same* session and
> would still inherit it. Everyday, no-wipe run/test isolation is the
> **epoch** axis, **shipped in v0.3.5** ([ISSUE-0085](ISSUE-0085-epoch-axis-run-isolation.md),
> RFC 0031 Phase 3b): a fresh `PERSATRIX_EPOCH` / `--epoch` inherits
> *nothing* across all five tiers. `make reset` is now the whole-stack
> nuke, not the run-isolation tool. F-3 as a *recall-bleed* defect is
> fixed here; F-3 as an *auto-isolation ergonomic* is closed by ISSUE-0085.
