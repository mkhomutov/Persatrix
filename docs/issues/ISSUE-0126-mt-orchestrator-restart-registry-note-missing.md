---
id: ISSUE-0126
summary: "Seven manual tests prescribe an orchestrator restart mid-arc, and none of them warns that the restart empties the in-memory agent registry. The v0.3.2 execution report (F-6) asked for exactly this note in an operator playbook and it was never written, so the same trap has re-cost live runs across three releases. PR #823 fixed one instance (MT-MEMORY-MULTIUSER-001); the other seven are unguarded. Subordinate to ISSUE-0125 — if re-registration lands, the correct action is to REMOVE these notes rather than write six more."
status: open
severity: low
area: docs/manual-tests
created: 2026-08-08
refs:
  - docs/manual-tests/MT-MEMORY-MULTIUSER-001.md
  - docs/manual-tests/v0.3.0-execution-report.md
  - docs/manual-tests/v0.3.2-execution-report.md
  - internal/registry/registry.go
  - internal/server/chat_handler.go
  - agents/server.py
---

## Summary

Every MT that says "restart the orchestrator" is a trap, and the note
that was supposed to say so was requested three releases ago and never
written.

## Context

The underlying behaviour is **ISSUE-0125** (the registry is in-memory,
agents `_self_register()` once at their own startup, nothing re-registers
on orchestrator reconnect). That issue owns the *fix*. This one owns the
**operator-facing gap that exists until the fix ships** — and the cleanup
after it does.

It has been recorded twice already, as an execution-report line each
time, which is where findings go to be forgotten:

- [v0.3.0 execution report](../manual-tests/v0.3.0-execution-report.md) —
  "agents register at startup only. After the MT-LOGS-001 Step 3 restart,
  `docker compose restart agent-…` was needed to repopulate
  `/api/v1/agents`. Operational pattern from v0.2.x; no regression."
- [v0.3.2 execution report](../manual-tests/v0.3.2-execution-report.md)
  F-6 — same finding, and it closes with the explicit ask: *"worth a note
  in the operator playbook."* That note was never written.

Seven MTs on `main` prescribe an orchestrator restart mid-arc. Checked
2026-08-08; **none** of them mentions the registry:

| MT | Line | Restart step |
|----|------|--------------|
| [MT-AUTONOMOUS-003](../manual-tests/MT-AUTONOMOUS-003.md) | 165 | "restart the orchestrator and let the schedule fire again" |
| [MT-COST-003](../manual-tests/MT-COST-003.md) | 81 | budget edit, "then restart the orchestrator" |
| [MT-COST-004](../manual-tests/MT-COST-004.md) | 83 | budget edit, "then restart the orchestrator" |
| [MT-IDLE-001](../manual-tests/MT-IDLE-001.md) | 88 | "restart the orchestrator **so the persona reconnects**" |
| [MT-LOGS-001](../manual-tests/MT-LOGS-001.md) | 257 | "restart the orchestrator (`Ctrl-C` then `make run` again)" |
| [MT-PERSONA-CONFIDENTIALITY-001](../manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md) | 48 | `channels.yaml` edit, "and restart the orchestrator" |
| [MT-PERSONA-RECALL-001](../manual-tests/MT-PERSONA-RECALL-001.md) | 260 | `docker compose restart orchestrator` — "a restart opens a NEW `session_id`" |

MT-IDLE-001 is the sharp one: it does not merely omit the warning, it
asserts the opposite — the persona does **not** reconnect, and that line
tells the operator to expect that it will. MT-LOGS-001 is the same Step 3
the v0.3.0 report was written about, still unguarded three releases later.

[MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md) was
the eighth; PR #823 gave it a precondition warning. That fixed one
instance of the class, not the class.

**The symptom is path-dependent, which is why a single copy-pasted
sentence will not do.** Any per-MT note has to say which of the two an
operator should expect:

- **Chat path** (`persatrix chat` → `POST /api/v1/agents/{id}/chat`) — the
  handler resolves the agent against the registry *before* it publishes
  ([`internal/server/chat_handler.go`](../../internal/server/chat_handler.go)),
  so an empty registry answers `404` and the CLI prints
  `error: 404 Not Found: agent not found`. Loud, immediate.
- **Channel-publish path** (`POST /api/v1/channels/{id}/messages`) — no
  such gate. The publish returns `201`, the dispatch is dropped with one
  `channels: dispatch target not registered` WARN, and the persona simply
  never replies. Near-silent.

## Impact

Low severity, real cost. It does not break the product — it burns **live
runs on paid providers**, which is where these MTs are executed, and it
has now done so across three release cycles. On the publish path it is
worse than a wasted run: a silent persona is indistinguishable from a
persona that legitimately recalled nothing, so an MT whose bar is an
*absence* can record a false PASS.

The meta-impact is the one worth naming: the finding has been correctly
diagnosed twice and both times filed somewhere nobody reads before a run.

## Proposed fix / investigation path

Order matters — check ISSUE-0125's status first.

1. **If ISSUE-0125 lands re-registration**, do *not* write seven notes.
   Delete the PR #823 warning from MT-MEMORY-MULTIUSER-001, confirm the
   restart steps are safe as written, and close this issue citing the
   commit. This is the preferred outcome.
2. **If it does not land in this train**, put the note in one shared place
   rather than seven — the operator playbook F-6 asked for, or a
   `docs/manual-tests/README.md` preamble — and have the seven MTs point
   at it. The content is already written: see the precondition block in
   MT-MEMORY-MULTIUSER-001 (symptom on both paths, wait-for-`/healthz`
   before restarting personas, credential needed on `GET /api/v1/agents`
   under `auth.mode: enabled`).
3. Either way, carry F-6's second half, which is also unrecorded outside
   that report: the RFC 0009 rate-limiter bucket is **not** flushed on
   orchestrator restart, so a post-restart turn can draw `429` for up to
   ~60 s independently of the registry.

## Notes

> 2026-08-08 — captured during the PR #823 review. Deliberately filed
> separately from that PR, which is scoped to one file. ISSUE-0125 is not
> on `main` yet (it sits on `fix/v03x-issue0125-agent-reregistration`), so
> it is referenced here by id only — no link, per the doc-links gate.
