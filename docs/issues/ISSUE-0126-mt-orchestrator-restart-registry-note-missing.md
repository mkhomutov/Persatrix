---
id: ISSUE-0126
summary: "Eight manual tests prescribe an orchestrator restart mid-arc, and none of them warns that the restart empties the in-memory agent registry. The v0.3.2 execution report (F-6) asked for exactly this note in an operator playbook and it was never written, so the same trap has re-cost live runs across three releases. PR #823 fixed one instance (MT-MEMORY-MULTIUSER-001); the other eight are unguarded. Subordinate to ISSUE-0125 — if re-registration lands, the correct action is to REMOVE these notes rather than write eight more."
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

The underlying behaviour is
**[ISSUE-0125](ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)**
(the registry is in-memory, agents `_self_register()` once at their own
startup, nothing re-registers on orchestrator reconnect). That issue owns the *fix*. This one owns the
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

Eight MTs on `main` prescribe an orchestrator restart mid-arc. Checked
2026-08-08 (MT-AUTH-001 added 2026-08-10 — see the Notes); **none** of
them mentions the registry:

| MT | Line | Restart step |
|----|------|--------------|
| [MT-AUTH-001](../manual-tests/MT-AUTH-001.md) | 128 | Leg 6 — `auth.mode: disabled` restore, "restart", then `persatrix chat <agent>` |
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

MT-AUTH-001 is the one where the failure is **mis-attributable**, which
is worse than a burnt turn. Leg 6's restart is followed immediately by
`persatrix chat <agent>`, and its pass criterion is that behaviour be
*"indistinguishable from pre-RFC-0039"*. An empty registry answers that
chat with `404 agent not found` — so the leg does not read as an
environment problem, it reads as a **regression in the feature under
test**, and the operator goes debugging RFC 0039. The same file restarts
twice more at line 82 (rebind to `0.0.0.0` to observe the residual WARNs,
then back to loopback before Leg 3).

[MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md) was
the ninth; PR #823 gave it a precondition warning. That fixed one
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

Order matters — check
[ISSUE-0125](ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)'s
status first.

1. **If
   [ISSUE-0125](ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)
   lands re-registration**, do *not* write eight notes.
   Delete the PR #823 warning from MT-MEMORY-MULTIUSER-001, confirm the
   restart steps are safe as written, and close this issue citing the
   commit. This is the preferred outcome.
2. **If it does not land in this train**, put the note in one shared place
   rather than eight — the operator playbook F-6 asked for, or a
   `docs/manual-tests/README.md` preamble — and have the eight MTs point
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
> separately from that PR, which is scoped to one file. ISSUE-0125 was
> not on `main` when this was written (it sat on
> `fix/v03x-issue0125-agent-reregistration`), so it was referenced by id
> only to keep the doc-links gate green.

> 2026-08-10 — ISSUE-0125 landed on `main` in
> [#824](https://github.com/mkhomutov/Persatrix/pull/824), so the by-id
> references above are now real links. Its fix section is the one to read
> before acting on option 1 here: the recommended shape is registration
> scoped to a live connection, which #824 filed as an amendment ask
> against RFC 0040 §C rather than a phase to wait on.

> 2026-08-10 — review fold-in (PR #825): the enumeration was one MT short.
> [MT-AUTH-001](../manual-tests/MT-AUTH-001.md) Leg 6 restores
> `auth.mode: disabled`, restarts, and chats — an orchestrator-only
> restart on the loud path, missed by the 2026-08-08 sweep because that
> pass keyed on the phrase "restart the orchestrator" and Leg 6 says only
> "restart". Counts corrected throughout (eight unguarded, MULTIUSER the
> ninth). An issue whose whole argument is that loose accounting is how
> this survives had better count correctly.
>
> The sweep that found it also cleared four near-misses, recorded here so
> the next reader does not re-derive them:
> [MT-CHANNEL-CONFIG-001:245](../manual-tests/MT-CHANNEL-CONFIG-001.md)
> reads "Restart the orchestrator" but the command is a full compose
> bounce, so the agents come back with it;
> [MT-CONSOLE-001:136](../manual-tests/MT-CONSOLE-001.md) and
> MT-CHANNEL-CONFIG-001:306 are orchestrator-only restarts whose
> assertions touch no persona; and
> [MT-CHANNEL-003:256](../manual-tests/MT-CHANNEL-003.md) describes a
> restart rather than prescribing one. The membership rule the table
> follows: **an orchestrator-only restart (not a full-stack bounce) with a
> persona-dependent assertion after it.**

> 2026-08-23 — **the count moves to ten MTs and TWO warnings.** The
> group-channel MT this workstream adds
> ([MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md))
> prescribes an orchestrator restart at Leg 0 and again at Leg 7, and it
> carries its own precondition warning — written for the same reason PR
> #823 wrote MT-MEMORY-MULTIUSER-001's, and after the trap had already
> cost a live arc on 2026-08-07. So the enumeration is now ten MTs, of
> which **two** are guarded and eight are not. Option 1 is unchanged in
> shape and one item longer: when [ISSUE-0125](ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)
> lands re-registration, delete **both** warnings and confirm the eight
> unguarded restart steps are safe as written.

