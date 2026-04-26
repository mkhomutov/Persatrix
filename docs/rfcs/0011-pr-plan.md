# RFC 0011 — PR Implementation Plan (Internal Channels — v0.3.0 scope) (scaffold)

**RFC**: [0011-channels-bridges.md](0011-channels-bridges.md)
**Created**: 2026-04-25
**Branch prefix**: `feature/v030-rfc0011-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

> **Status**: 🔨 Scaffold — PR rows have branch names, scopes, and dependency links pinned, but per-PR key-implementation-detail and tests sections are placeholders. Flesh out before the first implementation PR opens.

---

## Overview

RFC 0011 spans four phases for v0.3.0 (internal channels). External bridges are deferred to v0.5.0. This plan splits the v0.3.0 scope into **8 PRs** — the largest workstream in v0.3.0.

> **Estimate calibration**: 1.7× factor.

**Prerequisites**:
- [RFC 0008 PR plan](0008-pr-plan.md) PR 2 merged (`MemoryFacade` for task agents) — required by PR 5 (Phase 3 memory integration).
- [RFC 0020 PR plan](0020-pr-plan.md) PR 4 merged (summarize-on-close) — paired joint-delivery with PR 5 of this plan.
- [RFC 0009 PR plan](0009-pr-plan.md) PR 2 merged (rate-limit middleware) — required by PR 2 of this plan (REST publish endpoint).

**Cross-RFC sequencing**:
- **PR 2** (REST publish) gates on RFC 0009 PR 2 (rate-limit middleware) — see [RFC 0011 §Phase 1 — Dependencies](0011-channels-bridges.md#phase-1-channel-store-and-rest-routing). If RFC 0009 slips, this PR ships the startup-WARN path with an opt-out gate.
- **PR 5** is the **joint-delivery PR with [RFC 0020 PR plan](0020-pr-plan.md) PR 5** — both RFCs reference each other's PR number.

---

## Dependency Graph

```
PR 1 (Phase 1a — channels package + ChannelStore + SQLite migration + schema rewrite)
  ↓
PR 2 (Phase 1b — REST endpoints + ChannelRouter + config loading; depends on RFC 0009 PR 2)
  ↓
PR 3 (Phase 2a — proto regen: ChannelMessageEvent + ReceiveChannelMessage RPC; delete v0.2 ChannelService)
  ↓
PR 4 (Phase 2b — SEND_CHANNEL_MESSAGE action + Python servicer + response gate + DELETE endpoints)
  ↓
PR 5 (Phase 3 — joint with RFC 0020 PR 5: interaction-scoped channel memory; depends on RFC 0008 PR 2)
  ↓
PR 6 (Phase 4a — Rust CLI subcommands: list/join/send/reply/history/watch)
  ↓
PR 7 (Phase 4b — UserParticipant membership + watch polling + manual tests + docs)
  ↓
PR 8 (Review follow-ups + RFC partial-close — internal scope only; external bridges deferred to v0.5.0)
```

---

## PR Sequence

### PR 1: `feature/v030-rfc0011-channels-store` — Phase 1a: Channel Store + Schema

**Depends on**: Nothing (RFC 0008 / 0020 deps land later).
**Estimated size**: ~450–500 lines.

#### Scope (high-level)

- `internal/channels/` (rewritten from 7-line stub) — `Channel`, `ChannelMessage`, `ChannelStore` interface, SQLite implementation with migration runner.
- `schemas/channel.schema.json` rewritten in place with the new vocabulary (`group | dm | thread`).
- `config/channels.yaml` rewritten to match the new schema.
- Unit tests: CRUD, membership enforcement, history pagination, message-cap pruning, thread-FK cascade.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] ROADMAP.md row for RFC 0011 → `🚧 Implementing`
- [ ] Master Progress Overview row 6 → 🔄 In progress
- [ ] Schema `description` carries the "internal-only until v1.0" disclaimer per [OQ #9 resolution](0011-channels-bridges.md#open-questions)

---

### PR 2: `feature/v030-rfc0011-rest-routing` — Phase 1b: REST + Router + Config

**Depends on**: PR 1 + [RFC 0009 PR plan](0009-pr-plan.md) PR 2.
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `internal/server/handlers.go` — channel REST endpoints (create, list, publish, history, thread, add member). DELETE endpoints land in PR 4.
- `ChannelRouter` — publish-and-fanout logic; dispatches to registered agent gRPC addresses.
- `cmd/orchestrator/main.go` — config loading + router initialization.
- Rate-limit middleware applied to publish endpoint per RFC 0009 PR 2.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 3: `feature/v030-rfc0011-proto-regen` — Phase 2a: Proto + RPC

**Depends on**: PR 2.
**Estimated size**: ~250–400 lines.

#### Scope (high-level)

- `proto/task.proto` — `ChannelMessageEvent` + `ReceiveChannelMessage` RPC added to `AgentService`.
- `proto/agent_message.proto` — delete the v0.2-era `ChannelService`.
- Regenerate `internal/generated/`, `agents/generated/`.
- Delete `agents/server.py` v0.2 servicer registration; delete `agents/server_servicers.py::ChannelServiceServicer`.
- Delete `tests/unit/python/test_server_channel.py` (targets the deleted surface).

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 4: `feature/v030-rfc0011-agent-delivery` — Phase 2b: Action + Servicer + Gate

**Depends on**: PR 3.
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `agents/persona_types.py` — `EventType.CHANNEL_MESSAGE`, `ActionType.SEND_CHANNEL_MESSAGE`, `SendChannelMessageAction`.
- `agents/server_servicers.py` — `ReceiveChannelMessage` on `AgentServiceServicer`.
- `agents/dispatch.py` — `SEND_CHANNEL_MESSAGE` action executor (completes the existing scaffolding).
- Persona-runtime response gate (filters by `respond` policy: `when_mentioned` / `always` / `never`).
- DELETE endpoints (`DELETE /api/v1/channels/{id}`, `DELETE /api/v1/channels/{id}/members/{participant_id}`) with cascade tests.
- `internal/executor/dispatch.go` — `DispatchChannelMessage`.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] Two-agent integration test (one agent on `when_mentioned`, no mention → `channel.messages.gated` increments)

---

### PR 5: `feature/v030-rfc0011-memory-integration` — Phase 3 (joint with RFC 0020 PR 5)

**Depends on**: PR 4 + [RFC 0008 PR plan](0008-pr-plan.md) PR 2 + [RFC 0020 PR plan](0020-pr-plan.md) PR 4.
**Estimated size**: ~300–500 lines.

#### Scope (high-level)

- `CHANNEL_MESSAGE` event handler routes to `InteractionTracker.add_turn` (RFC 0020 §G).
- `MemoryFacade.retrieve_relevant` supports channel-scoped recall via `tags=[channel_id]`.
- Channel-history tier added to the `MemoryBudget` greedy fill (no change to `MemoryBudget` itself).
- Relationship memory: channel interactions increment count + influence trust score (per-interaction, not per-message — RFC 0020 contract).

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

#### PR checklist

- [ ] **Joint delivery** with [RFC 0020 PR plan](0020-pr-plan.md) PR 5 — both PRs reference each other's PR number
- [ ] Integration test: agent B reply demonstrates channel-history awareness

---

### PR 6: `feature/v030-rfc0011-cli-subcommands` — Phase 4a: Rust CLI

**Depends on**: PR 4 (REST API stable).
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `cli/src/commands/channel.rs` — `channel list/join/send/reply/history/watch` subcommands.
- `cli/src/main.rs` — register `channel` subcommand group.
- `--json` flag on `watch` per [OQ #4 resolution](0011-channels-bridges.md#open-questions).

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 7: `feature/v030-rfc0011-human-participation` — Phase 4b: Human + MT + Docs

**Depends on**: PR 6.
**Estimated size**: ~350–500 lines.

#### Scope (high-level)

- `UserParticipant` channel membership wired through `POST /api/v1/channels/{id}/members`.
- `persatrix channel watch` polling loop (5s default; `--interval` configurable).
- Manual tests: MT-CHANNEL-001 through MT-CHANNEL-006.
- Channels user guide + architecture diagram update.

#### Key implementation details *(TBD)*
#### Tests *(TBD)*

---

### PR 8: `feature/v030-rfc0011-close` — Review Follow-Ups + Internal-Scope Close

**Depends on**: PR 7.
**Estimated size**: ~150–300 lines.

| File | Change |
|------|--------|
| `docs/rfcs/0011-channels-bridges.md` | Status → `⚠️ Partially Implemented` (external bridges deferred to v0.5.0). |
| `ROADMAP.md` | RFC 0011 row → `⚠️ Partially Implemented (internal channels)`. |
| `docs/v0.3.0-plan.md` | Master Progress Overview row 6 → ✅. |

CHANGELOG.md is **deferred to v0.3.0 release prep** (Phase 4 PR 3).

#### PR checklist

- [ ] All deferred review findings addressed or downgraded
- [ ] `make test` passes; `make lint` clean

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| PR 2 publish endpoint ships without rate-limit middleware (RFC 0009 PR 2 slips) | Startup-WARN path with explicit opt-out gate; documented in [RFC 0011 §Phase 1 — Dependencies](0011-channels-bridges.md#phase-1-channel-store-and-rest-routing). Production deployments cannot accidentally enable without flipping the opt-out. |
| PR 5 joint delivery with RFC 0020 PR 5 slips | Documented divergence path (per-event episodic writes, backfilled in v0.3.x). Both PRs reference each other's PR number to make slippage visible. |
| Schema rewrite (`channel.schema.json`) breaks external tooling | Schema declared "not-yet-public" in v0.3.0 release notes per [OQ #9 resolution](0011-channels-bridges.md#open-questions); top-level description embeds the disclaimer. |
| Cascade depth saturation in tight-loop pair channels | Existing global `cascade_depth=5` backstop holds; per-channel override deferred per [OQ #11](0011-channels-bridges.md#open-questions). |

---

## ROADMAP Hygiene

- **PR 1 opens** → ROADMAP RFC 0011 → `🚧 Implementing`; Master Progress Overview row 6 → 🔄.
- **PR 8 merges** → ROADMAP RFC 0011 → `⚠️ Partially Implemented` (external bridges deferred); row 6 → ✅.

---

## Scaffold TODOs

Before opening PR 1:
- [ ] Fill in "Key implementation details" + "Tests" for each PR.
- [ ] Pin estimated sizes against the RFC's Files Touched table.
- [ ] Confirm the "joint delivery" PR pairing with [RFC 0020 PR plan](0020-pr-plan.md) PR 5 is reflected in both plans' PR 5 sections.
