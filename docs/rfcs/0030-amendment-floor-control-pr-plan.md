# RFC 0030 Floor-Control Amendment — PR Implementation Plan (v0.3.6 scope)

**Amendment**: [0030-amendment-floor-control-speaker-serialization.md](0030-amendment-floor-control-speaker-serialization.md)
**RFC**: [0030-multi-agent-conversation-governance.md](0030-multi-agent-conversation-governance.md) (Layer 2.5)
**Created**: 2026-06-03
**Branch prefix**: `feature/v036-floor-control-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.6-plan.md](../v0.3.6-plan.md) (folded in as a release blocker; the plan's fold-in row is omitted only because that file is at its size-check word budget — this plan is the authoritative workstream)

---

## Overview

The amendment fixes a release blocker: a channel message is fanned out to every responder **concurrently** ([`internal/channels/fanout.go`](../../internal/channels/fanout.go), fire-and-forget), so each persona composes against a transcript that contains none of its peers' replies — N overlapping, mutually-blind replies to one stimulus. The fix is **floor control** (Layer 2.5): serialize responders into a deterministic speaker round where each reads the prior speaker.

The work is **4 PRs**, almost entirely Go orchestrator-side, dark until PR 3 flips the per-channel flag default. PR 1 adds the floor registry + responder ordering with no behaviour change; PR 2 rewires `fanout` into the serialized loop behind the off-by-default flag; PR 3 defaults it on for group channels, documents it, and lands the manual test; PR 4 (fast-follow) adds telemetry. All five amendment decisions ([D1–D5](0030-amendment-floor-control-speaker-serialization.md#decisions-resolved)) are locked, so no design gate remains before PR 1.

**Prerequisite**: v0.3.0 channels stack (✅ released). Reuses the [`replyWaiter`](../../internal/channels/waiter.go) correlation table and the deterministic [`GetMembers`](../../internal/channels/sqlite_query.go) order — both already shipped.

### Decisions locked at plan-authoring time

All five resolve in the amendment ([Decisions](0030-amendment-floor-control-speaker-serialization.md#decisions-resolved)); mirrored here as the load-bearing constraints:

- **D1 — deferred fanout for floor-turn replies.** The round loop is the sole dispatcher; a floor-turn reply is persisted + `Notify`-ed but its `fanout` is skipped. Floor-mutex is the fallback if reply-correlation proves fragile in PR 2. **Implemented in PR 2.**
- **D2 — `floor_turn_timeout_seconds`, default 45s, config-overridable.** Distinct from the 5s `channelFanoutPerRecipientTimeout`. **Config in PR 1, consumed in PR 2.**
- **D3 — round order frozen at round start.** No mid-round mention promotion. **PR 1 ordering helper.**
- **D4 — no responder-per-round cap in v0.3.6.** Layers 0/2 bound the worst case. **No code; PR 4 telemetry is the revisit trigger.**
- **D5 — key floor state off `(channel_id, stimulus)`,** not `interaction_id` (RFC 0020 not wired Go-side). Re-key is a post-v0.3.6 follow-up. **PR 1 registry key.**

### Candidate responder set vs. delivery

A subtlety the loop must respect: `fanout` today dispatches to every non-`never` member for **two** reasons — to trigger a possible reply *and* to deliver the message for memory ingestion (the receiver response gate may suppress the reply but still ingests). Floor control only needs to serialize the **reply-producing** dispatches. So the loop splits members:

- **Candidate responders** = `always` members ∪ `when_mentioned` members that are mentioned ∪ thread-reply-to-self members. The orchestrator computes this best-effort from data it already holds: `respond_policy` (from `GetMembers`), `msg.Mentions`, and the pre-resolved `threadParentSenderID` ([router.go](../../internal/channels/router.go)). Correctness does not depend on perfect replication of the receiver gate — a candidate the gate ultimately suppresses just yields no reply and the 45s timeout advances the loop.
- **Non-responders** (`when_mentioned`, not mentioned) = delivered fire-and-forget as today, concurrently, for memory ingestion only. They are not in the floor queue.

Only candidate responders enter the serialized round; this keeps non-responders off the critical path and avoids paying a timeout per silent member.

### Sequencing

**Merge order: PR 1 → PR 2 → PR 3 → PR 4.** PR 1 is a no-behaviour-change addition (new file + inert config). PR 2 is the load-bearing rewire, flag-gated off, where the integration tests live. PR 3 is the user-visible flip + docs + manual test + status. PR 4 is independently mergeable telemetry and may trail the v0.3.6 tag.

---

## Dependency Graph

```
PR 1 (floor_control.go: registry + ordering; config/schema knobs, inert)
  ↓
PR 2 (rewire fanout → serialized loop behind flag; deferred-fanout (D1); 45s timeout (D2);
      integration tests)
  ↓
PR 3 (flag default on for group channels; docs/guides/channels.md; MT-CHANNEL-GOV-002;
      RFC 0030 + amendment status)
  ↓
PR 4 (telemetry: floor_turn{outcome}, round-duration histogram) — fast-follow, may trail the tag
```

PRs 1 and 4 carry no user-visible behaviour change on their own; PR 2 is dark behind the flag; PR 3 is the behaviour flip.

---

## PR Sequence

### PR 1: `feature/v036-floor-control-registry` — Floor registry + responder ordering (no behaviour change)

**Depends on**: v0.3.0 baseline.
**Purpose**: Land the pure data structures and helpers with full unit coverage, before any `fanout` rewire, so each diff is reviewable and bisectable.

| File | Change |
|------|--------|
| `internal/channels/floor_control.go` | **New.** (a) `floorRegistry` — per-channel floor with `acquire(channelID)` / `release(channelID)` so at most one round runs per channel at a time (keyed by `channel_id` per D5); in-process, mirroring `replyWaiter`'s single-replica constraint. (b) `orderResponders(members []Member, msg ChannelMessage, threadParentSenderID string) (responders, nonResponders []Member)` — splits the candidate responder set from non-responders (see §Candidate responder set), then orders responders **mentioned-first, then existing `GetMembers` order** (D3). Order is computed once and returned as a fixed slice. |
| `internal/channels/config.go` | Add `FloorControl bool` and `FloorTurnTimeoutSeconds int` (default 45 — D2) to the per-channel config struct; default `FloorControl=false`. |
| `schemas/channel.schema.json` | Add `floor_control` (bool) and `floor_turn_timeout_seconds` (int ≥ 1) keys; both optional with the defaults above. |
| `internal/channels/floor_control_test.go` | **New.** Registry: acquire blocks a second acquire on the same channel until release; release is safe/idempotent; distinct channels are independent. Ordering: mentioned-first; stable tie-break by member order; `when_mentioned`-not-mentioned excluded from responders but present in non-responders; thread-reply-to-self included; single-responder and empty degenerate cases. |

**Acceptance**: `go test ./internal/channels/...` green; no call site references the new file yet; `make validate` passes the schema additions.

---

### PR 2: `feature/v036-floor-control-loop` — The serialized floor loop, behind the flag

**Depends on**: PR 1.
**Purpose**: Rewire `fanout` to run the speaker round when floor control is enabled and there are ≥2 candidate responders. The behaviour-defining PR; ships dark (flag default off from PR 1).

| File | Change |
|------|--------|
| [`internal/channels/fanout.go`](../../internal/channels/fanout.go) | When `FloorControl` is on and `len(responders) >= 2`: deliver non-responders fire-and-forget as today, then run the **floor loop** over responders — `floorRegistry.acquire` → for each `r`: `replyWaiter.Register(channel, r)`, dispatch only to `r`, await the waiter or `FloorTurnTimeoutSeconds` (45s, D2), then advance → `release`. Otherwise (flag off, or <2 responders, or DM): the existing concurrent path, unchanged. |
| [`internal/channels/router.go`](../../internal/channels/router.go) | **Deferred fanout (D1):** mark the channel as having an active round while the loop holds the floor and record the current floor-holder. In `Publish`, when an inbound message is the active round's floor-holder reply on that channel, run the store commit and `r.waiter.Notify(msg)` but **skip** `r.fanout(...)` — the loop is the sole dispatcher and advances to the next responder with the reply now in history. Cross-*round* cascade stays bounded by `cascade_depth` (Layer 0). |
| `internal/channels/floor_control.go` | The loop body itself (driving registry + waiter + timeout), factored here and called from `fanout`. |
| `internal/channels/fanout_floor_test.go` | **New.** Integration: 3 `always` responders + 1 stimulus → exactly one in-flight dispatch at a time; responder 2's reconstructed transcript contains responder 1's reply, responder 3's contains both. Mention ordering: a stimulus mentioning C grants C the floor first. Deferred fanout: a floor-turn reply is persisted (visible via `GET /messages`) but spawns no competing fanout during the round. Timeout: a non-replying responder advances the loop after the (test-shortened) timeout. DM / single-responder: identical to pre-PR behaviour. |

**Acceptance**: with the flag forced on in tests, all integration assertions hold; with the flag off (default), existing channel tests pass unchanged.

**Fallback (D1)**: if reply-correlation across the REST publish boundary proves fragile, switch to the per-channel floor-mutex variant (guard `fanout` with the registry lock; accept reduced—not collapsed—amplification). The decision to switch, if needed, is made in PR 2 review with the failing case attached.

---

### PR 3: `feature/v036-floor-control-enable` — Default on for group channels + docs + manual test

**Depends on**: PR 2.
**Purpose**: The behaviour flip and the operator-facing surface.

| File | Change |
|------|--------|
| `config/channels.yaml` (template) + default resolution | Default `floor_control` **on for group channels**, n/a for DMs (single responder). Operators can override per channel. |
| [`docs/guides/channels.md`](../../docs/guides/channels.md) | New "Floor control" subsection: what serialization does, the 45s per-turn timeout, the flag, and the latency trade (responders go serial). |
| `docs/manual-tests/MT-CHANNEL-GOV-002.md` | **New.** Three personas in a group channel, one user prompt; expected: ordered, mutually-aware replies with at least one `DO_NOTHING` when a point is already covered — contrasted against the pre-amendment simultaneous-shout baseline. |
| [`0030-multi-agent-conversation-governance.md`](0030-multi-agent-conversation-governance.md) + [amendment](0030-amendment-floor-control-speaker-serialization.md) | Status hygiene: Layer 2.5 row → implemented; amendment status → ✅ Implemented (or ⚠️ Partially if PR 4 deferred past the tag). |
| ROADMAP / CHANGELOG | `[0.3.6]` entry; ROADMAP RFC index note that RFC 0030 Layer 2.5 shipped via the amendment. |

**Acceptance**: a fresh `--enable-ui` run with a multi-persona group channel shows ordered replies in the console timeline; `MT-CHANNEL-GOV-002` recorded.

---

### PR 4: `feature/v036-floor-control-telemetry` — Floor telemetry (fast-follow)

**Depends on**: PR 2 (independent of PR 3; may trail the v0.3.6 tag).
**Purpose**: Make the latency cost and timeout rate observable so D2 (timeout) and D4 (no cap) are data-driven.

| File | Change |
|------|--------|
| [`internal/observability/metrics/channel_instruments.go`](../../internal/observability/metrics/channel_instruments.go) | `channel.conversation.floor_turn{outcome=replied\|timeout}` counter; a floor-round-duration histogram (RFC 0019 naming). |
| `internal/channels/floor_control.go` | Emit the instruments at turn completion and round close. |

**Acceptance**: counters/histogram visible in the metrics endpoint under a multi-responder round.

---

## Test Strategy (summary)

- **Unit (PR 1)**: registry mutual-exclusion + independence; ordering split + mentioned-first + stable tie-break + degenerate cases.
- **Integration (PR 2)**: one-at-a-time dispatch; mutual visibility across the round; mention-driven floor order; deferred-fanout persistence-without-refanout; timeout advance; DM/single-responder no-op.
- **Manual (PR 3)**: `MT-CHANNEL-GOV-002` — ordered, mutually-aware multi-persona replies vs. the shout baseline.
- **Regression**: flag-off path runs the existing concurrent fanout suite unchanged (backward compatibility gate).

## Status & ROADMAP hygiene

- **PR 1–2 open** → amendment stays 📋 Proposed; no RFC index change (companion docs are excluded from `INDEX.md`).
- **PR 3 merges** → amendment → ✅ Implemented (or ⚠️ Partially if PR 4 trails); RFC 0030 Layer 2.5 row marked shipped; CHANGELOG `[0.3.6]`; `make rfcs` re-run if any front-matter changes.
- **v0.3.6 tag** → floor control is part of the release contract; if PR 4 trails, note it as a fast-follow in the release notes.

## Related documentation

- [RFC 0030 Amendment — Floor Control / Speaker Serialization](0030-amendment-floor-control-speaker-serialization.md) — the design and the locked decisions this plan implements.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — Layer 2.5's parent.
- [`internal/channels/fanout.go`](../../internal/channels/fanout.go), [`waiter.go`](../../internal/channels/waiter.go), [`sqlite_query.go`](../../internal/channels/sqlite_query.go) — the code this plan touches and reuses.
- [v0.3.6 Plan](../v0.3.6-plan.md) — the release this lands in.
