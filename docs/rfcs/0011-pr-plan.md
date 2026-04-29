# RFC 0011 — PR Implementation Plan (Internal Channels — v0.3.0 scope)

**RFC**: [0011-channels-bridges.md](0011-channels-bridges.md)
**Created**: 2026-04-25
**Last updated**: 2026-04-29
**Branch prefix**: `feature/v030-rfc0011-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

> **Status**: ✅ Ready — Per-PR key implementation details, tests, and checklists are pinned against the RFC's Phased Implementation Plan and Files Touched table. PR 1 may open once RFC 0009 PR 2 (rate-limit middleware) lands or its startup-WARN opt-out gate is agreed.

---

## Overview

RFC 0011 spans four phases for v0.3.0 (internal channels). External bridges are deferred to v0.5.0. This plan splits the v0.3.0 scope into **8 PRs** — the largest workstream in v0.3.0.

> **Estimate calibration**: 1.7× factor.

**Workstream prerequisite**: none — PR 1 has no upstream RFC dep (the channels store, schema, and SQLite migration are self-contained).

**Per-PR cross-RFC dependencies** (also pinned in each PR's Depends-on row):
- PR 2 → [RFC 0009 PR plan](0009-pr-plan.md) PR 2 (rate-limit middleware on the publish endpoint).
- PR 5 → [RFC 0008 PR plan](0008-pr-plan.md) PR 2 (`MemoryFacade` for channel-scoped recall).
- PR 5 → [RFC 0020 PR plan](0020-pr-plan.md) PR 4 (summarize-on-close — paired joint-delivery with this plan's PR 5).
- PR 5 → [RFC 0009 PR plan](0009-pr-plan.md) PR 3 (`InputSanitizer` on the channel-ingest path).

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
PR 5 (Phase 3 — joint with RFC 0020 PR 5: interaction-scoped channel memory; depends on RFC 0008 PR 2 + RFC 0009 PR 3)
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

#### Key implementation details

- Schema migration runner sets `PRAGMA foreign_keys = ON` at connection time (per [RFC 0011 §B](0011-channels-bridges.md#b-channel-store)) so both the `messages.thread_id` self-cascade and the `memberships`/`messages` → `channels(id)` cascades are enforced. Without this, the cap-boundary prune of a thread root surfaces FK violations and `DELETE /api/v1/channels/{id}` (PR 4) becomes undeliverable.
- `ChannelStore.GetOrCreateDM(a, b string)` is the **single source of truth** for DM ID canonicalization: lexicographically sort the two participant IDs before joining with `:`. Publish/history call sites never concat DM IDs themselves — they call this helper.
- Participant-ID validation lives at the registration boundary (loaders reject `:`, whitespace, non-ASCII) and `GetOrCreateDM` re-checks at runtime; no SQL `CHECK` constraint per RFC §A rationale.
- Per-channel cap (default 10,000) prunes oldest-first inside the same transaction as `PublishMessage`. Cascade on `thread_id` handles the straddling-thread case automatically.
- Global named-channel cap (default 50, `channels.max_channels`) — overflow at startup is a config validation error; runtime via REST returns 409 (REST endpoints land in PR 2; PR 1 only ships the store-side check).
- Schema rewrite carries the top-level `description` *"Internal-only schema until v1.0; `$id` may break across v0.x bumps without notice."* per [OQ #9 resolution](0011-channels-bridges.md#open-questions). Same path, same `$id` URL.
- Timestamp column type is `DATETIME` (Go-owned) — intentional split from RFC 0020 `episodes.started_at` (`REAL`/epoch-seconds, Python-owned). See RFC §B "Timestamp type".

#### Tests

- `ChannelStore` CRUD: create/get/list channels; add/get/remove members; idempotent re-add returns the same row.
- Membership enforcement: `IsMember` false → publish rejected at the store layer; member-list filters by `channel_id`.
- History pagination: `GetHistory(limit, before)` returns newest-first, strictly older than `before`; cursor walk reaches the oldest row without duplicates.
- Per-channel cap pruning: publish 10,001 messages → exactly 10,000 remain, oldest dropped.
- **Thread-FK cascade test** (called out in RFC §Test Strategy because it spans the cap boundary): publish 10,001 messages including a thread root that straddles the cap, force prune, assert zero orphaned reply rows and no FK violation.
- Channel-deletion cascade: deleting a channel removes its memberships and messages in one transaction; thread self-references inside the deleted set resolve transitively.
- DM canonicalization: `GetOrCreateDM("agent-b", "agent-a") == GetOrCreateDM("agent-a", "agent-b")`; both produce `dm:agent-a:agent-b`.
- Schema validation: `config/channels.yaml` validates against the rewritten `schemas/channel.schema.json`; legacy `direct`/`broadcast`/`meeting` configs fail validation with a clear message.
- Global channel cap: 51st named channel insert fails with a typed error.

#### PR checklist

- [ ] ROADMAP.md row for RFC 0011 → `🚧 Implementing`
- [ ] Master Progress Overview row 6 → 🔄 In progress
- [ ] Schema `description` carries the "internal-only until v1.0" disclaimer per [OQ #9 resolution](0011-channels-bridges.md#open-questions)
- [ ] `make validate` green against the rewritten `config/channels.yaml`
- [ ] `make test` Go suite green; thread-FK cascade test present and named so it cannot be silently dropped

---

### PR 2: `feature/v030-rfc0011-rest-routing` — Phase 1b: REST + Router + Config

**Depends on**: PR 1 + [RFC 0009 PR plan](0009-pr-plan.md) PR 2.
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `internal/server/handlers.go` — channel REST endpoints (create, list, publish, history, thread, add member). DELETE endpoints land in PR 4.
- `ChannelRouter` — publish-and-fanout logic; dispatches to registered agent gRPC addresses.
- `cmd/orchestrator/main.go` — config loading + router initialization.
- Rate-limit middleware applied to publish endpoint per RFC 0009 PR 2.

#### Key implementation details

- REST surface matches the [RFC §C endpoint table](0011-channels-bridges.md#c-message-routing-and-delivery) **minus** the two DELETE endpoints (deferred to PR 4 per the same table). Query parameters (`limit`, `before`) match the RFC §C "Query parameters" table; unrecognized parameters apply the listed default rather than 400.
- `ChannelRouter.Publish` is the single fanout entry point: validate sender membership, persist via `ChannelStore.PublishMessage`, look up subscribers, filter by `sender_id != subscriber_id`, then enqueue per-subscriber dispatch. Actual gRPC call to `ReceiveChannelMessage` is wired in PR 4 — PR 2 ships the router with a no-op dispatcher seam plus a fanout stub that increments `channel.messages.delivered{status="ok"}` against the registered address list.
- `channel_type` validation: router rejects publishes where the field disagrees with the `channel_id` prefix (RFC §C "`channel_type` proto-field redundancy" — orchestrator MUST validate on publish).
- Config + REST coexistence rules from [RFC §B](0011-channels-bridges.md#b-channel-store) are enforced at startup: REST-created-only channels preserved; membership disagreement between config and store is **loud failure** with the divergent participant IDs listed.
- Rate-limit middleware applied to `POST /api/v1/channels/{id}/messages` and `POST /api/v1/channels` per [RFC 0009 PR plan](0009-pr-plan.md) PR 2. If RFC 0009 PR 2 slips, ship the startup-WARN path gated by `security.rate_limit_enforced: false` (or equivalent CLI flag) — the choice between config knob and flag is decided in this PR's review.
- Thread-pre-resolution helper (`ChannelStore.GetMessage(id) → sender_id`) added here in preparation for PR 4's `thread_parent_sender_id` pre-resolution; lookup is `SELECT sender_id FROM messages WHERE id = ?` against the PK index.

#### Tests

- Unit: each REST handler — happy path, 404 on unknown channel, 403 on non-member publish, 409 on duplicate channel name, 409 on `max_channels` overflow.
- Unit: `ChannelRouter.Publish` validates `channel_type`/`channel_id` prefix agreement; mismatch returns a typed error and does **not** persist.
- Unit: pagination — `GET /api/v1/channels/{id}/messages?limit=N&before=T` honours both, returns newest-first.
- Integration: orchestrator starts with `config/channels.yaml`, channels and memberships visible via `GET /api/v1/channels`. Rerun → idempotent (no duplicate rows, no spurious membership inserts).
- Integration: config-vs-store divergence — pre-seed the store with a membership not in `channels.yaml`, restart, assert startup fails loudly listing the divergent participant ID.
- Integration: rate-limit middleware engaged on the publish endpoint; aggressive `curl` loop receives 429 once the per-agent quota is exhausted (uses RFC 0009 PR 2 fixtures). If RFC 0009 PR 2 hasn't merged, this case ships behind the startup-WARN opt-out and is exercised by a separate test asserting the WARN log fires.
- Integration: REST publish round-trips through the router and lands in `messages` with the canonical DM `channel_id` even when a caller sends the participants in reverse order.

#### PR checklist

- [ ] RFC 0009 PR 2 merged **or** startup-WARN opt-out path landed and documented in `docs/v0.3.0-plan.md`
- [ ] `make validate` green against `config/channels.yaml`
- [ ] All Phase 1 manual smoke (`curl` create/publish/history) documented in PR description

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

#### Key implementation details

- `ChannelMessageEvent` field shape matches the [RFC §C proto block](0011-channels-bridges.md#c-message-routing-and-delivery) exactly — `channel_type` carried as a string (no proto enum per [OQ #10](0011-channels-bridges.md#open-questions)); `thread_id` is the empty string when not a reply (proto-3 default rather than `oneof`).
- `ReceiveChannelMessage` returns the existing `TaskAck` — no new ack message added.
- Delete order: (1) regenerate stubs against the new `proto/`; (2) drop the v0.2 servicer registration in `agents/server.py` (L41 import + L133–134 `add_*Servicer_to_server` call per RFC §Files Touched); (3) delete `agents/server_servicers.py::ChannelServiceServicer`; (4) delete `tests/unit/python/test_server_channel.py` in the same commit so CI never sees a missing-import failure window.
- This PR ships **no** new servicer logic — `AgentServiceServicer.ReceiveChannelMessage` is a stub returning `TaskAck(success=True)`. Real handler lands in PR 4.
- Generated-file regeneration runs through `make proto` with no manual edits; review diff is restricted to `proto/` + the generated trees.

#### Tests

- `make proto` regenerates without diff drift (re-running produces no further changes).
- `pytest agents/tests/` and `go test ./...` green after the deletes — confirms no surviving import paths reference `ChannelServiceServicer` or `agent_message.proto`'s `ChannelService`.
- Unit: stub `ReceiveChannelMessage` returns `TaskAck(success=True)` and increments no metrics (real wiring is PR 4).
- Lint: `ruff` + `mypy` clean; no orphaned imports in `agents/server.py` / `agents/server_servicers.py`.

#### PR checklist

- [ ] `make proto` is the only path used to regenerate; manual edits to `internal/generated/` or `agents/generated/` rejected in review
- [ ] Old `ChannelService`-related code deleted in the same commit as the proto edit (no "deprecation window" — the surface had no producer)

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

#### Key implementation details

- Renames per the [RFC §Relationship to Existing Scaffolding](0011-channels-bridges.md#relationship-to-existing-scaffolding) disposition: `EventType.MESSAGE_RECEIVED` → `CHANNEL_MESSAGE`; `ActionType.SEND_MESSAGE` → `SEND_CHANNEL_MESSAGE`. `MENTION` retained as a derived convenience event the gate may emit on self-mentions.
- `AgentEvent` extension is **additive only** per [RFC §D](0011-channels-bridges.md#d-agent-integration): adds top-level `thread_id: str | None`. `respond_policy` and `thread_parent_sender_id` stay in `payload`. `timestamp` keeps its `float` Unix-epoch-seconds shape (no rename).
- `ChannelRouter.Publish` (PR 2) extended here to pre-resolve `thread_parent_sender_id` once per publish via the PR-2 `ChannelStore.GetMessage` helper; value is identical for every recipient and amortizes the lookup across fanout.
- **Response gate** lives in the persona runtime (agent-side), pre-LLM, before memory recall. Three branches per [RFC §D table](0011-channels-bridges.md#d-agent-integration): `when_mentioned` triggers on `agent_id ∈ event.mentions` **or** thread-reply-to-self (`event.thread_id != None and event.payload["thread_parent_sender_id"] == self.id`); `always` triggers except `sender_id == self.id`; `never` always suppresses. Suppressed events still persist for memory ingestion in PR 5 — PR 4 increments `channel.messages.gated{policy}` only.
- Defense-in-depth ordering preserved: gate (primary) → existing `EventDispatcher.max_cascade_depth=5` (backstop) → REST-side rate limit. Outbound `SEND_CHANNEL_MESSAGE` actions carry `cascade_depth + 1` like `MESSAGE_RECEIVED` does today.
- `ActionExecutor.execute(SendChannelMessageAction)` calls `POST /api/v1/channels/{id}/messages` with `sender_id` injected by the framework from the agent's registered ID — agents cannot spoof another sender. `_MAX_MENTIONS_PER_ACTION` cap and `no_targets` status taxonomy from the existing `_handle_send_message` are preserved.
- DELETE endpoints rely on the PR-1 cascade columns; handler is thin (membership check → `DELETE FROM channels WHERE id = ?`). Removing a participant does **not** delete that participant's prior messages — `messages.sender_id` retains the historical value per [RFC §C endpoint table](0011-channels-bridges.md#c-message-routing-and-delivery).
- `internal/executor/dispatch.go::DispatchChannelMessage` reuses the existing gRPC connection pool and per-call timeout from `MESSAGE_RECEIVED` dispatch — at-most-once delivery, no retry on failure (recovery via history endpoint per RFC §C "Delivery guarantees").

#### Tests

- Unit (Python): `EventType.CHANNEL_MESSAGE` + `ActionType.SEND_CHANNEL_MESSAGE` round-trip through the action/event encoders.
- Unit (Python): response gate — table-driven over `(policy, mentions, thread_id, thread_parent_sender_id, sender_id)` covering all three policies plus thread-reply-to-self vs. thread-reply-to-other.
- Unit (Python): self-message filter — agent never receives `CHANNEL_MESSAGE` where `sender_id == self.id`, regardless of policy (orchestrator-side filter; verified via `ChannelRouter.Publish` test).
- Unit (Python): `_MAX_MENTIONS_PER_ACTION` cap preserved; over-cap action returns the existing `no_targets` status without invoking the REST client.
- Unit (Go): `DispatchChannelMessage` happy path + offline-subscriber path (gRPC error → `channel.messages.delivered{status="failed"}` increment, no retry).
- Unit (Go): DELETE handlers — cascade removes memberships + messages; participant-removal preserves their prior messages.
- **Two-agent integration test** (RFC §Phase 2 deliverable 7): three agents on `#planning`. A on `always`, B on `when_mentioned`, C on `when_mentioned` and not mentioned. A publishes a message that mentions B. Assert: B receives + responds; C receives the event but `channel.messages.gated{policy="when_mentioned"}` increments and no `SEND_CHANNEL_MESSAGE` is dispatched.
- Integration: `cascade_depth=5` backstop — synthetic always-respond pair with cascade_depth pre-set to 5 produces zero further dispatches; gate is bypassed by depth check.
- Integration: thread-reply-to-self triggers `when_mentioned` even without an explicit mention (uses pre-resolved `thread_parent_sender_id`).

#### PR checklist

- [ ] Two-agent integration test (one agent on `when_mentioned`, no mention → `channel.messages.gated` increments)
- [ ] Self-mention through `MENTION` derived event verified or explicitly deferred
- [ ] `cascade_depth` backstop test green (drop happens upstream of the gate, regardless of policy)

---

### PR 5: `feature/v030-rfc0011-memory-integration` — Phase 3 (joint with RFC 0020 PR 5)

**Depends on**: PR 4 + [RFC 0008 PR plan](0008-pr-plan.md) PR 2 + [RFC 0020 PR plan](0020-pr-plan.md) PR 4 + [RFC 0009 PR plan](0009-pr-plan.md) PR 3.
**Estimated size**: ~300–500 lines.

#### Scope (high-level)

- `CHANNEL_MESSAGE` event handler routes to `InteractionTracker.add_turn` (RFC 0020 §G).
- `MemoryFacade.retrieve_relevant` supports channel-scoped recall via `tags=[channel_id]`.
- Channel-history tier added to the `MemoryBudget` greedy fill (no change to `MemoryBudget` itself).
- Relationship memory: channel interactions increment count + influence trust score (per-interaction, not per-message — RFC 0020 contract).
- `InputSanitizer.Sanitize()` applied to inbound channel message content on the ingest path before it reaches `InteractionTracker.add_turn` and before storage — closes the integration anticipated by [RFC 0009 PR plan](0009-pr-plan.md) PR 3 and the [RFC 0011 §Security Considerations](0011-channels-bridges.md#security-considerations) mitigation.

#### Key implementation details

- **Joint delivery** with [RFC 0020 PR plan §PR 5](0020-pr-plan.md#pr-sequence). RFC 0011 PR 5 owns: channel-message ingest path, sanitization, scope-keying call into `InteractionTracker.add_turn`, channel-history tier in the persona-runtime memory caller, channel-scoped `retrieve_relevant` filter. RFC 0020 PR 5 owns: `InteractionTracker` per-channel scoping rules + non-`closed`-row defense-in-depth filter. Both PRs reference each other's PR number.
- Scope key per [RFC 0020 §G](0020-interaction-lifecycle.md): DM = participant pair (sorted), thread = `thread_id`, group = `(channel_id, local_agent_id)` rolling. Pinned in this PR's review against the RFC 0020 PR 5 scope to keep the two implementations from drifting.
- Per-event episodic writes are **explicitly not introduced** here. Channel turns flow through `InteractionTracker.add_turn`; the single closed-interaction episode is written by RFC 0020 PR 4 machinery on close. No `store_episode` call from the channel handler.
- `MemoryFacade.retrieve_relevant(query, *, scope="channel", tags=[event.channel_id], limit=_CHANNEL_RECALL_LIMIT)` per [RFC §E](0011-channels-bridges.md#e-memory-integration). `_CHANNEL_RECALL_LIMIT` defaults to 20 and is exposed as `optimization.yaml → channels.recall_limit`.
- Channel-history tier slot in the `MemoryBudget.try_add` order matches the **canonical cross-RFC priority order** in [RFC §E](0011-channels-bridges.md#e-memory-integration): relationship → open commitments → **channel history (this RFC, only on `CHANNEL_MESSAGE`)** → episodic recall → recent notes → duration priors. No change to `MemoryBudget` itself; this PR only edits the persona-runtime caller.
- Relationship updates: `record_interaction` runs on **interaction close**, not per-message — preserves the RFC 0020 PR 4 contract. Channel interactions feed the same trust-score path as RFC 0016 direct chats.
- `InputSanitizer.Sanitize()` is applied **once on ingest**, before `add_turn` and before persistence. The audit-event side channel from RFC 0009 PR 3 fires on every inbound message regardless of mutation result. Outbound `SEND_CHANNEL_MESSAGE` content is **not** sanitized again — agents are trusted producers within the deployment; sanitization protects the inbound boundary.
- DM-privacy invariant from [RFC §Security Considerations](0011-channels-bridges.md#security-considerations) holds: DM content is stored in each participant's isolated episodic memory, never in a shared store visible to other agents.

#### Tests

- Integration: ten-message exchange between agents A and B in `#planning` produces **one closed-interaction episode per agent** on close (agent A's view, agent B's view) — not ten per-event episodes. Asserts the joint-delivery contract.
- Integration: agent B's reply to A's message demonstrates channel-history awareness — verifiable by injecting a distinct token in A's earlier message and asserting it surfaces in B's `MemoryBudget` admitted set on the next turn.
- Integration: channel history admitted via `MemoryBudget.try_add` after relationship + commitments and before episodic recall (priority-order assertion).
- Integration: relationship trust score increments **once per closed interaction**, not once per message (RFC 0020 contract regression test).
- Integration: `InputSanitizer.Sanitize()` applied on ingest — synthetic prompt-injection payload is stripped/flagged before the `add_turn` call; sanitization audit event fires (RFC 0009 PR 3 fixture).
- Integration: thread interaction closes on `StructuralCloseDetector` fire (thread archive) — paired with RFC 0020 PR 5's test of the same path.
- Integration: DM privacy — agent C in the same deployment but not party to A↔B DM cannot retrieve A↔B episodic content via `retrieve_relevant`.
- Integration: `respond: never` listener still ingests memory (one closed-interaction episode written) but emits no `SEND_CHANNEL_MESSAGE`.

#### PR checklist

- [ ] **Joint delivery** with [RFC 0020 PR plan](0020-pr-plan.md) PR 5 — both PRs reference each other's PR number
- [ ] Integration test: agent B reply demonstrates channel-history awareness
- [ ] Channel-ingest path applies `InputSanitizer.Sanitize()` per [RFC 0009 PR plan](0009-pr-plan.md) PR 3; sanitization audit event fires on every inbound message
- [ ] No per-event `store_episode` call from the channel handler (regression guard against the duplicate-summary problem)
- [ ] Cross-RFC priority order in the persona-runtime caller matches [RFC §E](0011-channels-bridges.md#e-memory-integration) and [RFC 0021 §J](0021-persona-temporal-awareness.md#j-token-budget-integration) verbatim

---

### PR 6: `feature/v030-rfc0011-cli-subcommands` — Phase 4a: Rust CLI

**Depends on**: PR 4 (REST API stable).
**Estimated size**: ~400–500 lines.

#### Scope (high-level)

- `cli/src/commands/channel.rs` — `channel list/join/send/reply/history/watch` subcommands.
- `cli/src/main.rs` — register `channel` subcommand group.
- `--json` flag on `watch` per [OQ #4 resolution](0011-channels-bridges.md#open-questions).

#### Key implementation details

- Output formats per the [RFC §F output-format table](0011-channels-bridges.md#f-human-participant-channels) — plain text default, structured JSON behind `--json`. Field shapes match `schemas/channel.schema.json` + `ChannelMessage` (RFC §A) so `persatrix channel history --json | jq` is portable.
- Thin-client pattern (per [`.github/instructions/rust-cli.instructions.md`](../../.github/instructions/rust-cli.instructions.md)): subcommands marshal args into REST calls and print the response. No client-side caching, no business logic.
- `channel send` and `channel reply` accept repeated `--mention <id>` flags and a `--mention-all` shorthand that expands to every channel member (per RFC §D — addresses the human-in-the-loop case).
- `channel watch` is the polling form — `GET /api/v1/channels/{id}/messages?before=<now>&limit=N` on a 5s default interval (configurable via `--interval`). Maintains a client-side high-watermark on the message ID just printed so the next poll filters duplicates. Polling cadence and the `--json` flag both pinned by [OQ #4](0011-channels-bridges.md#open-questions); SSE streaming deferred per [OQ #6](0011-channels-bridges.md#open-questions).
- `clap` v4 derive-style parser per repo convention; exhaustive `match` on subcommands; `tokio` for the async REST client.
- All subcommands return non-zero exit on REST error; error body is rendered to stderr, never to stdout (so `--json` callers can pipe stdout safely).
- `--mention-all` resolution is **client-side** (CLI fetches `GET /api/v1/channels/{id}` member list and expands locally) — keeps the orchestrator publish endpoint shape unchanged.

#### Tests

- Unit (Rust): each subcommand's arg-parsing happy path + missing-required-arg failure (uses `clap`'s test harness).
- Unit (Rust): JSON output shape per subcommand matches the [RFC §F table](0011-channels-bridges.md#f-human-participant-channels) — golden-file assertions on serialized examples.
- Unit (Rust): `--mention-all` expands to the channel's current member list (mocked `GET /api/v1/channels/{id}`).
- Integration: `cargo test` against a fixture orchestrator — `channel list` / `send` / `history` round-trip; exit codes correct on REST error.
- Integration: `channel watch` polling loop emits each new message exactly once; client-side watermark survives `--interval` ticks without duplicates.

#### PR checklist

- [ ] `cargo clippy --all-targets -- -D warnings` clean
- [ ] `cargo fmt --check` clean
- [ ] CLI help text reviewed against existing `persatrix logs` / `persatrix chat` voice

---

### PR 7: `feature/v030-rfc0011-human-participation` — Phase 4b: Human + MT + Docs

**Depends on**: PR 6.
**Estimated size**: ~350–500 lines.

#### Scope (high-level)

- `UserParticipant` channel membership wired through `POST /api/v1/channels/{id}/members`.
- `persatrix channel watch` polling loop (5s default; `--interval` configurable).
- Manual tests: MT-CHANNEL-001 through MT-CHANNEL-006.
- Channels user guide + architecture diagram update.

#### Key implementation details

- `UserParticipant` reuses the existing RFC 0016 `Participant` abstraction unchanged. Membership flows through the same `POST /api/v1/channels/{id}/members` endpoint that agents use — no human-specific path.
- Human messages obey the same response gate as agents per [OQ #7 resolution](0011-channels-bridges.md#open-questions) (option **b**): default `when_mentioned`, with `--mention-all` on `persatrix channel send` covering the broadcast case in 1-human-N-agent channels. Silent agents in DM-shaped channels are recoverable; flooding agents on every casual message is not.
- Manual tests live under [`docs/manual-tests/`](../manual-tests/) with the `MT-CHANNEL-NNN` numbering convention. Suggested coverage (one per ID, refined in PR review):
  - MT-CHANNEL-001: human creates `#planning`, joins as participant, sends a top-level message, sees one agent reply.
  - MT-CHANNEL-002: human DMs an agent; round-trip; verify DM canonicalization (publish with reversed participant order resolves to the same channel).
  - MT-CHANNEL-003: thread reply — human replies to an agent's message; agent's `when_mentioned` thread-reply branch fires.
  - MT-CHANNEL-004: `--mention-all` broadcast — every channel member responds (or is gated explicitly per their policy).
  - MT-CHANNEL-005: `channel watch` polling — second terminal observes messages from the first within one polling interval.
  - MT-CHANNEL-006: channel deletion via `DELETE /api/v1/channels/{id}` — cascade removes memberships + messages; `channel list` no longer shows it.
- Channels user guide goes under [`docs/guides/`](../guides/) with the existing voice (concise, command-driven, screenshots only where decisive). Architecture diagram update lands in [`docs/diagrams/`](../diagrams/) — the publish HTTP/REST hop + gRPC fanout asymmetry from [RFC §C](0011-channels-bridges.md#c-message-routing-and-delivery) is the centerpiece.
- Concurrent-publish ordering disclaimer (per [OQ #5](0011-channels-bridges.md#open-questions)) called out in the user guide; missed-message recovery contract (on-startup last-N fetch per [OQ #8](0011-channels-bridges.md#open-questions)) documented alongside.

#### Tests

- Integration: `UserParticipant` joins a channel via REST and shows up in `GET /api/v1/channels/{id}` member list with `respond_policy=when_mentioned`.
- Integration: human-published message flows through the agent gate (no implicit mention-all) — single-agent reply on explicit `--mention <agent>`, zero replies otherwise.
- Integration: `channel watch` polling — second client observes messages within `--interval` seconds; no duplicates across polls.
- Manual: MT-CHANNEL-001 … MT-CHANNEL-006 executed against a local docker-compose deployment; results captured in PR description.
- Docs lint: `make validate` green; new guide passes the existing markdown link check.

#### PR checklist

- [ ] All six manual tests have a dedicated file under `docs/manual-tests/` and exit-criteria checklist
- [ ] Channels user guide reviewed against `docs/documentation-guide.md`
- [ ] Architecture diagram update lands in this PR (not deferred)
- [ ] OQ #4 (`watch --json`), OQ #7 (human gate-bypass), OQ #8 (missed-message recovery) resolution wording surfaced in the user guide

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

## Sizing Risks and Contingent Splits

Estimated sizes above include the 1.7× calibration factor. The two PRs most at risk of crossing the [BRANCHING.md](../BRANCHING.md) 500-line soft cap:

- **PR 1 (channels store + schema)**: SQLite migration + `ChannelStore` interface + per-channel cap pruning + thread-FK cascade test. If the implementation crosses 500 lines, split into PR 1a (`ChannelStore` + schema migration + CRUD/membership tests) and PR 1b (per-channel cap pruning + global cap check + thread-FK cascade test). 1b depends on 1a; total dependency chain length grows by one but no other plan is affected.
- **PR 4 (action + servicer + gate + DELETE)**: largest persona-runtime touch + DELETE endpoints + integration test. If implementation crosses the cap, split into PR 4a (proto-side wiring: action type, servicer, dispatcher, basic delivery test) and PR 4b (response gate + DELETE endpoints + two-agent gate integration test). PR 5's joint-delivery dependency moves to 4b.

PRs 2, 3, 5, 6, 7 are within the calibrated band and are not pre-committing to a split.

## Cross-Plan Confirmations

- **Joint delivery** with [RFC 0020 PR plan](0020-pr-plan.md) PR 5 is reflected in both plans' PR 5 sections (this plan's PR 5 ↔ RFC 0020 PR 5). Either both merge in the same window or both ship the documented divergence path (per-event episodic writes here, backfill in v0.3.x).
- **Channel-ingest sanitization** is anticipated by [RFC 0009 PR plan](0009-pr-plan.md) PR 3 — both plans now point at the same integration site (PR 5 here, PR 3 there).
- **Memory facade tag/scope contract** is consumed at PR 5 from [RFC 0008 PR plan](0008-pr-plan.md) PR 2's frozen `compress`/`tags` API surface.
