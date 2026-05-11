# RFC 0011 — PR Implementation Plan (Internal Channels — v0.3.0 scope)

**RFC**: [0011-channels-bridges.md](0011-channels-bridges.md)
**Created**: 2026-04-25
**Last updated**: 2026-05-05 (PR 4a-ii-α merged as #249; PR 4a-ii-β-1 merged as #250; PR 4a-ii-β-2 merged as #251)
**Branch prefix**: `feature/v030-rfc0011-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.0-plan.md Phase 1 (combined plans PR)](../v0.3.0-plan.md#phase-1--author-the-six-rfc-pr-plans)

> **Status**: ✅ Ready — Per-PR key implementation details, tests, and checklists are pinned against the RFC's Phased Implementation Plan and Files Touched table. PR 2 may open once RFC 0009 PR 2 (rate-limit middleware) lands or its startup-WARN opt-out gate is agreed. (PR 1 has no upstream RFC dependency — see Dependency Graph.)

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

## Amendments

- [Chat as DM Channel (RFC 0016 unification)](0011-amendment-chat-as-dm.md) — 2026-05-04, landed inside PR 4a.
- [Cascade-depth wire propagation](0011-amendment-cascade-depth-wire-propagation.md) — 2026-05-11, lands in [v0.3.0 channel test-findings PR plan](../v0.3.0-test-findings-pr-plan.md) PRs 1–4.

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
  ↓ (PR 5 ‖ PR 6 — both depend only on PR 4; the Phase 3 / Phase 4a split below is conservative)
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

- [x] ROADMAP.md row for RFC 0011 → `🚧 Implementing`
- [x] Master Progress Overview row 6 → 🔄 In progress
- [x] Schema `description` carries the "internal-only until v1.0" disclaimer per [OQ #9 resolution](0011-channels-bridges.md#open-questions)
- [x] `make validate` green against the rewritten `config/channels.yaml`
- [x] `make test` Go suite green; thread-FK cascade test present and named so it cannot be silently dropped (`TestSQLiteStore_ThreadFKCascade`)

> **✅ Merged as PR #231 (2026-04-29).**

#### PR #231 review follow-ups

Deep review completed (local-only, not committed per [Status Hygiene rules](../development-workflow.md#status-hygiene)). No Must-Fix; the four Should-Fix items below are dispatched to the PRs where the fix is cheapest, before downstream consumers freeze the contract.

| # | Finding | Target PR | Rationale |
|---|---------|-----------|-----------|
| SF-1 | `buildDSN()` ([sqlite.go#L99-L106](../../internal/channels/sqlite.go#L99-L106)) concatenates `path + "?" + q.Encode()` and silently drops every PRAGMA when `path` is a `file:` URI (e.g. `file::memory:?cache=shared`, which the function's own doc-comment advertises). | ~~**PR 8**~~ ✅ **Resolved as [ISSUE-0049](../issues/ISSUE-0049-builddsn-drops-pragmas-on-file-uri-paths.md)** | Tracked as ISSUE-0049; fix splits `path` on `?` and merges caller-supplied query params into the PRAGMA `url.Values` (option 2 — preserves the doc-comment's `file::memory:?cache=shared` promise). Regression test pins the fix and a bare-path guard. |
| SF-2 | `CreateChannel()` ([sqlite.go#L184-L212](../../internal/channels/sqlite.go#L184-L212)) does not assert `ch.ID == "group:" + ch.Name` for `ChannelTypeGroup`. PR 2's REST handler is the first external caller and can desync the canonical address from the row PK. | **PR 2** | Cheapest before REST surface ships: either compute `ch.ID = "group:" + ch.Name` inside `CreateChannel` (canonical-id authority) or guard with `ErrInvalidChannelType`. |
| SF-3 | `PublishMessage()` ([sqlite_messages.go#L31-L70](../../internal/channels/sqlite_messages.go#L31-L70)) does not run `validateParticipantID` over `msg.Mentions`. The response gate (PR 4) treats `agent_id ∈ event.mentions` as a trigger; junk values become defense-in-depth gaps. | **PR 4** | Validate at the store boundary in the same PR that wires the response gate, so the gate's contract is end-to-end. |
| SF-4 | `channels.name TEXT NOT NULL UNIQUE` with the id-as-placeholder shim for DM/thread couples the schema to a reader convention ([sqlite.go#L124-L138](../../internal/channels/sqlite.go#L124-L138), [#L226-L232](../../internal/channels/sqlite.go#L226-L232), [#L259-L264](../../internal/channels/sqlite.go#L259-L264)). | **PR 2** | Cheap to change while no production data exists; switch to `name TEXT` plus `CREATE UNIQUE INDEX … ON channels(name) WHERE channel_type='group'` and drop the `if name != ch.ID` branches. |

Nice-to-Have items (also pinned to PR 8 unless an earlier PR's diff naturally includes the file): `PRAGMA user_version = 1` baseline in `applySchema`; soft byte cap on `msg.Content` at the store boundary; FK-disambiguation test for the "channel deleted concurrently" branch (needs a test-only mutation seam); rename or tighten `TestSQLiteStore_Close_Idempotent`; `db.Stats().MaxOpenConnections == 1` invariant test; `BeforeConnect`-style hook for `foreign_keys = ON` paired with the PR 2 `MaxOpenConns` lift; `Mentions` JSON-special-character round-trip test.

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
- **PR #231 review SF-2** — make `CreateChannel` the canonical-id authority: either compute `ch.ID = "group:" + ch.Name` inside the store (preferred) or guard with `if ch.Type == ChannelTypeGroup && ch.ID != "group:"+ch.Name { return ErrInvalidChannelType }`. Lands in this PR so the new REST handler cannot insert a row whose PK disagrees with its display name.
- **PR #231 review SF-4** — collapse the `name`-as-placeholder coupling before REST traffic exists: schema migration changes `channels.name` to `TEXT` (nullable) and adds `CREATE UNIQUE INDEX ux_channels_name ON channels(name) WHERE channel_type='group'`; readers (`GetChannel`, `ListChannels`) drop the `if name != ch.ID` branches. Gated by the migration runner work this PR introduces (see `applySchema` `TODO(rfc0011-pr2+)`) — `PRAGMA user_version` jumps from `0`→`2` (or `1`→`2` if PR 8's NTH-1 baseline lands first).

#### Tests

- Unit: each REST handler — happy path, 404 on unknown channel, 403 on non-member publish, 409 on duplicate channel name, 409 on `max_channels` overflow.
- Unit: `ChannelRouter.Publish` validates `channel_type`/`channel_id` prefix agreement; mismatch returns a typed error and does **not** persist.
- Unit: pagination — `GET /api/v1/channels/{id}/messages?limit=N&before=T` honours both, returns newest-first.
- Unit: SF-2 regression — `CreateChannel(Channel{Type: "group", ID: "group:foo", Name: "bar"})` is rejected (or normalised); `ID == "group:" + Name` invariant pinned.
- Unit: SF-4 regression — two `group` rows cannot share a `name` (partial unique index fires); a `dm` row with `name = NULL` and a `group` row with `name = "<dm-id>"` coexist without conflict.
- Integration: orchestrator starts with `config/channels.yaml`, channels and memberships visible via `GET /api/v1/channels`. Rerun → idempotent (no duplicate rows, no spurious membership inserts).
- Integration: config-vs-store divergence — pre-seed the store with a membership not in `channels.yaml`, restart, assert startup fails loudly listing the divergent participant ID.
- Integration: rate-limit middleware engaged on the publish endpoint; aggressive `curl` loop receives 429 once the per-agent quota is exhausted (uses RFC 0009 PR 2 fixtures). If RFC 0009 PR 2 hasn't merged, this case ships behind the startup-WARN opt-out and is exercised by a separate test asserting the WARN log fires.
- Integration: REST publish round-trips through the router and lands in `messages` with the canonical DM `channel_id` even when a caller sends the participants in reverse order.

#### PR checklist

- [x] RFC 0009 PR 2 merged **or** startup-WARN opt-out path landed and documented in `docs/v0.3.0-plan.md`
- [x] `make validate` green against `config/channels.yaml`
- [x] All Phase 1 manual smoke (`curl` create/publish/history) documented in PR description
- [x] New metrics (`channel.messages.delivered{status}`) registered in [docs/observability.md](../observability.md) and any dashboard manifests
- [x] PR #231 review SF-2 closed: `CreateChannel` enforces `ID == "group:" + Name` (or computes it) for `ChannelTypeGroup`
- [x] PR #231 review SF-4 closed: `channels.name` migrated to nullable + partial unique index on `channel_type='group'`; `GetChannel`/`ListChannels` placeholder shim removed; `user_version` bumped

> **RFC 0009 PR 2 status (PR #245)**: rate-limit middleware is wired
> generically through `WithRateLimiter` (RFC 0009 PR 2 / PR #244 merged)
> but the channels publish endpoint runs on the **startup-WARN
> opt-out** path for v0.3.0 — the channels REST surface is
> intentionally unauthenticated this release (token auth lands in
> RFC 0009 Phase 4) and the orchestrator emits a one-shot
> `channels: REST surface is UNAUTHENTICATED in v0.3.0 …` Warn
> whenever the channels subsystem is enabled. See CHANGELOG entry
> for the operator-facing trust-boundary statement.

> **✅ Merged as PR #245 (2026-05-04).**

#### PR #245 review follow-ups

Deep review completed (local-only, not committed per [Status Hygiene rules](../development-workflow.md#status-hygiene)). No Must-Fix applied at merge time. Seven follow-up issues captured as `docs/issues/ISSUE-0009` through `ISSUE-0015` (committed in the pre-merge tidy-up):

| Issue | Finding | Target PR | Severity |
|-------|---------|-----------|---------|
| [ISSUE-0009](../issues/ISSUE-0009-channel-fallback-warn-once-test-race.md) | `channelFallbackWarnOnce` is a package-level `sync.Once`; reassigned in tests causes latent `-race` flake once any sibling adopts `t.Parallel()`. | ~~**PR 3 or PR 8**~~ ✅ **Resolved PR 3 (#246)** | Medium |
| [ISSUE-0010](../issues/ISSUE-0010-reconcile-membership-divergence-doc-behaviour-mismatch.md) | `membershipDivergence` doc claims policy drift "is logged" but function only compares participant id sets — doc/behaviour mismatch. | **PR 8** | Low |
| [ISSUE-0011](../issues/ISSUE-0011-publish-mentions-count-cap.md) | `handlePublishMessage` forwards `req.Mentions` without count cap — defense-in-depth gap on the unauthenticated REST surface. | **PR 4** | Low |
| [ISSUE-0012](../issues/ISSUE-0012-channels-db-parent-dir-not-auto-created.md) | `--channels-db` default path (`data/channels.db`) parent directory not auto-created; fresh checkout silently degrades channels to 503. | ~~**PR 3**~~ ✅ **Resolved PR 3 (#246)** | Low |
| [ISSUE-0013](../issues/ISSUE-0013-channel-messages-published-counter.md) | No `channel.messages.published` counter alongside `channel.messages.delivered`; delivered/published ratio dashboard not computable. | **PR 4** | Low |
| [ISSUE-0014](../issues/ISSUE-0014-channel-fanout-bounded-concurrency.md) | `ChannelRouter.fanout` dispatches inline per-recipient (O(N × 5s) worst-case); bounded-concurrency `errgroup` needed before PR 4 gRPC dispatcher. | **PR 4** | Low |
| [ISSUE-0015](../issues/ISSUE-0015-list-channels-cursor-and-store-side-limit.md) | `handleListChannels` loads all rows then client-truncates; no `next_cursor` in response; silent data truncation once the channel cap is lifted. | **PR 8** | Low |

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
- `ReceiveChannelMessage` returns a new minimal `TaskAck { bool success, string error_message }` introduced in this PR (the v0.2-era `agent_message.proto` carried a different `MessageAck`; no pre-existing `TaskAck` symbol existed). v0.3.0 uses at-most-once delivery semantics — `success=false` means the agent did not process the event and the orchestrator does not retry.
- Delete order: (1) regenerate stubs against the new `proto/`; (2) drop the v0.2 servicer registration in `agents/server.py` (L41 import + L133–134 `add_*Servicer_to_server` call per RFC §Files Touched); (3) delete `agents/server_servicers.py::ChannelServiceServicer`; (4) delete `tests/unit/python/test_server_channel.py` in the same commit so CI never sees a missing-import failure window.
- This PR ships **no** new servicer logic — `AgentServiceServicer.ReceiveChannelMessage` is a stub returning `TaskAck(success=False, error_message="ReceiveChannelMessage handler not yet implemented (RFC 0011 PR 4)")`. Real handler lands in PR 4. (Original plan said `success=True`; revised after PR #246 deep review finding H1 — see [agents/server_servicers.py](../../agents/server_servicers.py) `ReceiveChannelMessage` docstring.)
- Generated-file regeneration runs through `make proto` with no manual edits; review diff is restricted to `proto/` + the generated trees.

#### Tests

- `make proto` regenerates without diff drift (re-running produces no further changes).
- `pytest agents/tests/` and `go test ./...` green after the deletes — confirms no surviving import paths reference `ChannelServiceServicer` or `agent_message.proto`'s `ChannelService`.
- Unit: stub `ReceiveChannelMessage` returns `TaskAck(success=False, error_message=...)` and increments no metrics (real wiring is PR 4a-i). Coverage moved to [tests/unit/python/test_receive_channel_message.py](../../tests/unit/python/test_receive_channel_message.py) when the real handler landed; the v0.2 channel-surface import-guard moved to [tests/unit/python/test_v02_channel_surface_removed.py](../../tests/unit/python/test_v02_channel_surface_removed.py).
- Lint: `ruff` + `mypy` clean; no orphaned imports in `agents/server.py` / `agents/server_servicers.py`.

#### PR checklist

- [x] `make proto` is the only path used to regenerate; the `proto-python` target now auto-rewrites absolute imports to relative form (Makefile fixup added in PR 3 review; ISSUE-0016 ✅ resolved)
- [x] Old `ChannelService`-related code deleted in the same commit as the proto edit (no "deprecation window" — the surface had no producer)

> **✅ Merged as PR #246 (2026-05-04).**

#### PR #246 review follow-ups

Deep review completed (local-only, not committed per [Status Hygiene rules](../development-workflow.md#status-hygiene)). One Must-Fix applied inline (H-1); no blocking findings at merge. Six follow-up issues captured in `docs/issues/`:

| Issue | Finding | Target PR | Severity |
|-------|---------|-----------|----------|
| [ISSUE-0018](../issues/ISSUE-0018-channel-message-event-receiver-bounds-enforcement.md) | `ChannelMessageEvent` wire bounds (content 4 000 chars, `mentions[]` 10 entries, `thread_id` 128 chars, `channel_type` membership) documented in proto comments only; receiver enforcement is PR 4's single gate. | **PR 4** | Medium |
| [ISSUE-0019](../issues/ISSUE-0019-taskack-reuse-policy-comment.md) | `TaskAck` named generically but used by exactly one RPC; proto reuse-policy comment needed to prevent scope-creep coupling. | **PR 8** | Low |
| [ISSUE-0020](../issues/ISSUE-0020-channel-type-proto-enum.md) | `ChannelMessageEvent.channel_type` is a closed string set `{group, dm, thread}`; promote to proto enum to eliminate per-receiver re-validation. Companion to ISSUE-0018. | **PR 8** | Low |
| [ISSUE-0021](../issues/ISSUE-0021-channel-message-event-roundtrip-test.md) | No round-trip serialization test for `ChannelMessageEvent`; field-number renumber accidents would survive CI. | **PR 4** | Low |
| [ISSUE-0022](../issues/ISSUE-0022-chatresponse-timestamp-format-divergence-comment.md) | `ChannelMessageEvent.timestamp` (int64 epoch) diverges from `ChatResponse`/`TaskProgress` (RFC 3339 string); cross-reference comment needed to prevent future regressions. | **PR 8** | Low |
| [ISSUE-0023](../issues/ISSUE-0023-ci-gate-make-proto-no-diff.md) | No CI gate for `make proto && git diff --exit-code`; blocked on ISSUE-0016 (✅ resolved) + ISSUE-0017 for the gate to pass cleanly. | **PR 8** | Low |

---

### PR 4: `feature/v030-rfc0011-agent-delivery` — Phase 2b: Action + Servicer + Gate

**Depends on**: PR 3.
**Estimated size**: ~700–900 lines (revised upward 2026-05-04 after the chat-as-DM unification — see [RFC 0011 amendment](0011-amendment-chat-as-dm.md)). **Splits into PR 4a-i, PR 4a-ii, and PR 4b** (the original PR 4a was further split 2026-05-04 — keeping the additive servicer separate from the cross-cutting chat-path migration sized each slice into the squash-friendly review-window). The cross-cutting `EventType.MESSAGE_RECEIVED` → `CHANNEL_MESSAGE` and `ActionType.SEND_MESSAGE` → `SEND_CHANNEL_MESSAGE` rename now also migrates the RFC 0016 chat ingest/reply path (heavy producer of the old names since v0.2.1) and must land atomically with the new servicer to avoid a window where chat is broken on `main`.

#### Scope split (PR 4a-i / PR 4a-ii / PR 4b)

- **PR 4a-i** (~250 lines, branch `feature/v030-rfc0011-receive-channel-message`): **additive** `EventType.CHANNEL_MESSAGE` + `ActionType.SEND_CHANNEL_MESSAGE` enum members (old names retained for chat) + additive top-level `AgentEvent.thread_id` + `ReceiveChannelMessage` Python servicer real handler (replaces PR 3's `success=False` stub) with proto-bound validation + strong-ref task set per PR #246 Should-Fix #2 + single-agent-per-process disambiguation taxonomy. No chat-path changes; no dispatch executor; no response gate. ✅ **Merged as PR #248 (2026-05-05).**
- **PR 4a-ii** (~700–900 lines combined; further split 2026-05-05 into PR 4a-ii-α + PR 4a-ii-β once exploration of the call-site fan-out — 15+ Python sites, plus the architectural shift from in-process `EventDispatcher` to REST/gRPC for cross-process channel delivery — confirmed the original ~300–400 line estimate was unreachable in a single squash-merge):
  - **PR 4a-ii-α** (~300–400 lines, branch `feature/v030-rfc0011-channel-message-rename`): hard renames `EventType.MESSAGE_RECEIVED` → `CHANNEL_MESSAGE` and `ActionType.SEND_MESSAGE` → `SEND_CHANNEL_MESSAGE` across Python (persona_types, dispatch, chat_reply, server_servicers, persona_runtime/*, llm_persona_agent format_event, all tests, docs/glossary). `_handle_send_message` renamed to `_handle_send_channel_message` and updated to emit `CHANNEL_MESSAGE` events; routing stays in-process via the Python `EventDispatcher` so chat keeps working unchanged on the renamed constants. Plus PR #231 review SF-3: `PublishMessage` validates every `msg.Mentions` entry via `validateParticipantID` before INSERT. **Atomic-rename safety**: chat ingest and chat-reply extraction migrate in the same PR so chat is never broken on `main`. **Out of scope here**: cross-process Go-side `MessageDispatcher` (deferred to 4a-ii-β); REST-based action executor (deferred to 4a-ii-β); chat-as-DM façade (deferred to 4a-ii-β). ✅ **Merged as PR #249 (2026-05-05).**
  - **PR 4a-ii-β** (~400–500 lines combined; further split 2026-05-05 into PR 4a-ii-β-1 + PR 4a-ii-β-2 once design exploration confirmed the chat-as-DM façade requires net-new publish-and-await infrastructure (per-pending-request reply waiter table + user-participant dispatch hook) that is architecturally distinct from the agent-side gRPC dispatcher, and bundling the two would push the diff past the 500-line review window):
    - **PR 4a-ii-β-1** (~250–350 lines, branch `feature/v030-rfc0011-grpc-dispatcher`): real Go `MessageDispatcher` implementation (`internal/channels/grpc_dispatcher.go` — registry-aware gRPC `ReceiveChannelMessage` invocation replacing `NoopDispatcher{}` in `cmd/orchestrator/channels.go`) + Python `_handle_send_channel_message` rewired from in-process `EventDispatcher` to `POST /api/v1/channels/{id}/messages` (sender_id framework-injected). Agent-to-agent channel delivery becomes cross-process. Chat path remains unchanged (still uses synchronous `SendChatMessage` gRPC); chat-as-DM façade deferred to β-2. ✅ **Merged as PR #250 (2026-05-05).**
    - **PR 4a-ii-β-2** (~200–300 lines, branch `feature/v030-rfc0011-chat-as-dm`): chat-as-DM rewrite per the [RFC 0011 amendment](0011-amendment-chat-as-dm.md) — `POST /api/v1/agents/{id}/chat` migrated from the legacy `SendChatMessage` gRPC round-trip to a channels-based publish-and-await façade. New `internal/channels/waiter.go` (`replyWaiter` — O(1) correlation table keyed on `(channel_id, sender_id)`, buffered cap-1 reply channel, `ErrWaiterAlreadyRegistered` sentinel, cancel-guard against fresh-registration clobber) plus `ChannelRouter.PublishAndAwait(ctx, msg, awaitFromAgentID, timeout)` (registers waiter **before** `Publish` to close the race window, `defer cancel`, `select` on reply / timer / `ctx.Done()`); `ErrChatTimeout` sentinel mapped to HTTP 504. `internal/server/chat_handler.go` rewritten — validates `agent_id` against `resourceIDRegex`, bounds message ≤ 4 000 chars, defaults `user_id` to `"local"`, clamps timeout to `[1s, 300s]`, mints `session_id` when absent; builds `ChannelMessage{ChannelID: dm.ID, SenderID: userID, Content, Mentions: [agentID]}` and delegates to `channelRouter.PublishAndAwait`; `GetOrCreateDM` errors discriminated 400 (caller-id violations) vs 500 (storage faults) per review M-1. **JSON contract preserved** — `chatRequest` / `chatResponse` shape unchanged so Rust CLI and existing REST clients are unaffected. `chatExecutor` wiring in `cmd/orchestrator/main.go` and `agents/server_servicers.py::SendChatMessage` intentionally left intact for binary-upgrade safety (now dead code; cleanup deferred to ISSUE-0035). Depends on β-1's gRPC dispatcher (so the agent's reply round-trips back through the new transport). Review M-1 applied inline (`GetOrCreateDM` error discrimination); four follow-ups filed: ISSUE-0033 (single-shot waiter multi-message reply, Medium → PR 4b), ISSUE-0034 (fanout warn noise on non-agent recipient, Medium → PR 4b), ISSUE-0035 (`chatExecutor` dead-but-wired cleanup, Low → PR 8), ISSUE-0036 (`doc_links.py` prefer `git ls-files`, Low → PR 8). ✅ **Merged as PR #251 (2026-05-05).**
- **PR 4b** (`feature/v030-rfc0011-response-gate`, ~700 lines): persona-runtime response gate (3 policies + thread-reply-to-self + DM override) for non-DM channels, additive proto fields (`respond_policy`, `thread_parent_sender_id`) carrying the gate inputs from publish to dispatch, `MessageDispatcher` signature lift to `DispatchEnvelope` (per-recipient `Member` + pre-resolved thread-parent), `ChannelStore.RemoveMember` + DELETE `/api/v1/channels/{id}` and `/api/v1/channels/{id}/members/{participant_id}` endpoints with cascade tests, `channel.messages.gated{policy}` Python counter, gate unit tests (table-driven over policy × mentions × thread × sender_id), `cascade_depth` backstop test, validator coverage for the new proto fields. Out of scope: ISSUE-0011/13/14/18/21/33/34 follow-ups (carried to PR 8 along with the two-agent integration test which depends on PR 5's memory-ingestion plumbing).

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
- **PR #231 review SF-3** — `ChannelStore.PublishMessage` runs `validateParticipantID` over `msg.Mentions` before INSERT; invalid entries return `fmt.Errorf("mentions[%d]: %w", i, err)`. Lands here so the response gate's `agent_id ∈ event.mentions` trigger has an end-to-end contract from REST boundary through store to gate, with no junk values reaching the gate.

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

- [x] Two-agent integration test (one agent on `when_mentioned`, no mention → `channel.messages.gated` increments) — landed in PR 4b (#252)
- [x] Self-mention through `MENTION` derived event verified or explicitly deferred — addressed in PR 4a-i (#248) via `_format_event` mention handling
- [x] `cascade_depth` backstop test green (drop happens upstream of the gate, regardless of policy) — landed in PR 4b (#252)
- [x] New metrics (`channel.messages.gated{policy}`) registered in [docs/observability.md](../observability.md) and any dashboard manifests — landed in PR 4b (#252)
- [x] PR #231 review SF-3 closed: `PublishMessage` validates every `msg.Mentions` entry via `validateParticipantID`; regression test covers invalid id rejection + JSON-special-character round-trip — closed in PR 4a-ii-α (#249)

> **PR 4 split status (2026-05-06)**: All sub-PRs merged — 4a-i (#248) `ReceiveChannelMessage` real handler + additive enums; 4a-ii-α (#249) hard rename `MESSAGE_RECEIVED`/`SEND_MESSAGE` → `CHANNEL_MESSAGE`/`SEND_CHANNEL_MESSAGE` + SF-3 mentions validation; 4a-ii-β-1 (#250) Go gRPC `MessageDispatcher` + Python `HTTPChannelPublisher`; 4a-ii-β-2 (#251) chat-as-DM rewrite (`replyWaiter` + `ChannelRouter.PublishAndAwait`); 4b (#252) response gate + DELETE endpoints. Canonical PR 4 (agent-delivery scope) is **closed**.

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
- **On-startup catch-up fetch** (resolves [RFC 0011 OQ #8](0011-channels-bridges.md#open-questions)): on persona-runtime boot, each subscribed channel is queried for the last 50 messages (`GET /api/v1/channels/{id}/messages?limit=50`); messages newer than the agent's `last_seen_at` per channel are replayed through the same `CHANNEL_MESSAGE` ingest path (sanitization + `InteractionTracker.add_turn`) but **without** triggering an outbound `SEND_CHANNEL_MESSAGE` (replay-mode flag bypasses the response gate). Best-effort — failure to reach the REST surface logs WARN and continues; watermark + per-tick recovery deferred until operational data justifies (RFC OQ #8).

#### Key implementation details

- **Joint delivery** with [RFC 0020 PR plan §PR 5](0020-pr-plan.md#pr-sequence). RFC 0011 PR 5 owns: channel-message ingest path, sanitization, scope-keying call into `InteractionTracker.add_turn`, channel-history tier in the persona-runtime memory caller, channel-scoped `retrieve_relevant` filter. RFC 0020 PR 5 owns: `InteractionTracker` per-channel scoping rules + non-`closed`-row defense-in-depth filter. Both PRs reference each other's PR number.
- Scope key per [RFC 0020 §G](0020-interaction-lifecycle.md): DM = participant pair (sorted), thread = `thread_id`, group = `(channel_id, local_agent_id)` rolling. Pinned in this PR's review against the RFC 0020 PR 5 scope to keep the two implementations from drifting.
- Per-event episodic writes are **explicitly not introduced** here. Channel turns flow through `InteractionTracker.add_turn`; the single closed-interaction episode is written by RFC 0020 PR 4 machinery on close. No `store_episode` call from the channel handler.
- `MemoryFacade.retrieve_relevant(query, *, scope="channel", tags=[event.channel_id], limit=_CHANNEL_RECALL_LIMIT)` per [RFC §E](0011-channels-bridges.md#e-memory-integration). `_CHANNEL_RECALL_LIMIT` defaults to 20 and is exposed as `optimization.yaml → channels.recall_limit`.
- Channel-history tier slot in the `MemoryBudget.try_add` order matches the **canonical cross-RFC priority order** in [RFC §E](0011-channels-bridges.md#e-memory-integration): relationship → open commitments → **channel history (this RFC, only on `CHANNEL_MESSAGE`)** → episodic recall → recent notes → duration priors. No change to `MemoryBudget` itself; this PR only edits the persona-runtime caller.
- Relationship updates: `record_interaction` runs on **interaction close**, not per-message — preserves the RFC 0020 PR 4 contract. Channel interactions feed the same trust-score path as RFC 0016 direct chats.
- `InputSanitizer.Sanitize()` is applied **once on ingest**, before `add_turn` and before persistence. The audit-event side channel from RFC 0009 PR 3 fires on every inbound message regardless of mutation result. Outbound `SEND_CHANNEL_MESSAGE` content is **not** sanitized again at the producer — every cross-agent hop traverses a receiving agent's inbound boundary, and that boundary re-runs the sanitizer. The model is *single-ingest sanitization at every consumer*, not *trust the producer*: prompt-injection that makes an LLM emit adversarial content is still caught when the next agent ingests it.
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

- [x] **Joint delivery** with [RFC 0020 PR plan](0020-pr-plan.md) PR 5 — both PRs reference each other's PR number
- [x] Channel-ingest path applies `sanitize()` once at the runtime boundary with `source=CONTEXT_SOURCE_CHANNEL_MESSAGE` (Python mirror of RFC 0009 §C); flagged content does not short-circuit ingest
- [x] Suppressed events (response-gate `respond=False`) still call `_store_event_episode` so memory captures the channel turn — without this, a `respond: when_mentioned` listener loses every non-mention from its memory
- [x] No per-event `store_episode` call from the channel handler (regression guard against the duplicate-summary problem) — single ingest path through `_store_event_episode` regardless of gate decision
- [x] Integration test: agent B reply demonstrates channel-history awareness — landed in the channel-history-tier follow-up sub-PR (tier wired into `MemoryBudget.try_add` between relationship and episodic; per-channel scope filter via shared `recall_with_scope_filter` helper; `tests/integration/test_channel_history_awareness.py` pins the agent-B-reply contract)
- [x] Cross-RFC priority order in the persona-runtime caller matches [RFC §E](0011-channels-bridges.md#e-memory-integration) and [RFC 0021 §J](0021-persona-temporal-awareness.md#j-token-budget-integration) verbatim — pinned by `tests/unit/python/test_memory_context_priority_order.py` (the test name itself cites both spec sections so a future re-order cannot survive review without a deliberate name change)
- [x] On-startup catch-up fetch implemented (last-50-per-channel REST query, replay-mode flag suppresses outbound `SEND_CHANNEL_MESSAGE`); resolves [RFC 0011 OQ #8](0011-channels-bridges.md#open-questions) — landed in the catch-up-fetch follow-up sub-PR: new `agents/channel_catchup.py` (`replay_channel_history`, `DEFAULT_CATCHUP_LIMIT=50`) + replay-mode short-circuit in `agents/persona_runtime/action_loop.py::_on_event_inner` (after sanitize, before gate; defense-in-depth `sender_id == agent_id` skip) + `channel.messages.replayed` counter (`replay_attrs(channel_id=…)`) + `AgentServer.start` wires `_replay_channel_history` after `_self_register` over the shared `aiohttp.ClientSession`
- [x] Catch-up fetch coverage — agent restarts mid-conversation, fetches recent messages, replays through ingest, **no** outbound responses to replayed messages. Originally scoped as a cross-process integration test; landed as a three-test unit suite that exercises the same contract end-to-end via a loopback `aiohttp` orchestrator + spy persona agent: `tests/unit/python/test_replay_mode_action_loop.py` (replay-mode short-circuit), `tests/unit/python/test_channel_catchup.py` (REST round-trip + membership filter + oldest-first ordering + best-effort error paths), and `tests/unit/python/test_server_catchup_wiring.py` (startup wiring + persona-only filter + shared-session contract). A real cross-process integration test against `bin/persatrix-server` is deferred to the v0.4.0 watermark + per-tick recovery work where it is the natural surface to exercise. (PR-265 review L6: checklist wording corrected — "integration test" was the original phrase but the delivered coverage is unit-level + loopback HTTP.)

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

- [x] `cargo clippy --all-targets -- -D warnings` clean
- [x] `cargo fmt --check` clean
- [x] CLI help text reviewed against existing `persatrix logs` / `persatrix chat` voice
- [x] `scripts/checks/file_size.py --strict` clean — `ChannelCommands` clap enum + `dispatch` fn co-located with the bodies in `cli/src/commands/channel.rs` (under cap), wire DTOs split into `channel_types.rs`, helper tests externalised via `#[path = "channel_tests.rs"]` so the runtime file stays under the 500-line review cap
- [x] `cargo test` green: 70 tests pass (12 new channel module helper tests + 8 new channel REST DTO serde-contract tests)

#### Deferred to a follow-up

- **Integration tests against a fixture orchestrator** (RFC plan "Integration: cargo test against a fixture orchestrator — list/send/history round-trip" + "channel watch polling loop emits each new message exactly once"). The Rust CLI has no in-tree integration-test harness against `bin/persatrix-server` today — `cli/tests/` does not exist; the existing precedent (`cli/src/commands/logs.rs`'s SSE consumer) is unit-tested only. The channel surface ships with the same coverage shape: pure-helper unit tests + golden-file serde-contract tests pin every wire-shape decision, with end-to-end coverage delivered by the manual MT-CHANNEL suite documented under [`docs/manual-tests/`](../manual-tests/) — [MT-CHANNEL-001](../manual-tests/MT-CHANNEL-001.md) (list/join), [MT-CHANNEL-002](../manual-tests/MT-CHANNEL-002.md) (send/reply/history), and [MT-CHANNEL-003](../manual-tests/MT-CHANNEL-003.md) (watch poll/dedup/full-page warning) collectively cover every flag combination on the PR 6 CLI surface against a docker-composed orchestrator + four agents. The cross-process `cargo test` harness arrives naturally with PR 7's docker-compose fixture; tracked as the PR 6 follow-up there.

---

### PR 7: `feature/v030-rfc0011-human-participation` — Phase 4b: Human + MT + Docs

**Depends on**: PR 6.
**Estimated size**: ~350–500 lines (docs-heavy; manual-test files run long but each remains under the [BRANCHING.md](../BRANCHING.md) review-window cap independently).

#### Scope (high-level)

- ✅ `UserParticipant` channel membership wired through `POST /api/v1/channels/{id}/members` (already shipped in earlier PRs — `agents/participant.py` carries the abstraction; the `channel join --as <user>` CLI surface lands in PR 6).
- ✅ `persatrix channel watch` polling loop (already shipped in PR 6; covered by [MT-CHANNEL-003](../manual-tests/MT-CHANNEL-003.md)).
- Manual tests: MT-CHANNEL-001 through MT-CHANNEL-006.
- Channels user guide + architecture diagram update.

#### Key implementation details

- `UserParticipant` reuses the existing RFC 0016 `Participant` abstraction unchanged. Membership flows through the same `POST /api/v1/channels/{id}/members` endpoint that agents use — no human-specific path.
- Human messages obey the same response gate as agents per [OQ #7 resolution](0011-channels-bridges.md#open-questions) (option **b**): default `when_mentioned`, with `--mention-all` on `persatrix channel send` covering the broadcast case in 1-human-N-agent channels. Silent agents in DM-shaped channels are recoverable; flooding agents on every casual message is not.
- Manual tests live under [`docs/manual-tests/`](../manual-tests/) with the `MT-CHANNEL-NNN` numbering convention. Final coverage as landed (numbering shifted from the original suggestion table because PR 6 authored the CLI-surface tests as 001–003):
  - [MT-CHANNEL-001](../manual-tests/MT-CHANNEL-001.md) (PR 6): `channel list` / `join` CLI surface against the docker-composed orchestrator (covers human-as-participant join via `--as`).
  - [MT-CHANNEL-002](../manual-tests/MT-CHANNEL-002.md) (PR 6): `channel send` / `reply` / `history` CLI surface (covers `--mention`, `--mention-all` sender exclusion, threaded reply, JSON shape).
  - [MT-CHANNEL-003](../manual-tests/MT-CHANNEL-003.md) (PR 6): `channel watch` polling, dedup, and full-page-warning coverage.
  - [MT-CHANNEL-004](../manual-tests/MT-CHANNEL-004.md) (this PR): human-mentions-agent end-to-end (live LLM reply via `ember-owl` + non-mention gate-suppression sanity check). Closes the v0.3.0 user-facing promise's "human posts and the agent replies" hot path.
  - [MT-CHANNEL-005](../manual-tests/MT-CHANNEL-005.md) (this PR): DM canonicalization round-trip — REST publish from `(alice, bob)` and `(bob, alice)` both resolve to `dm:alice:bob`; reverse-order create is idempotent.
  - [MT-CHANNEL-006](../manual-tests/MT-CHANNEL-006.md) (this PR): channel deletion via `DELETE /api/v1/channels/{id}` (cascade) and `DELETE /channels/{id}/members/{participant_id}` (membership only; prior messages preserved).
- Channels user guide goes under [`docs/guides/`](../guides/) with the existing voice (concise, command-driven, screenshots only where decisive). Architecture diagram update lands in [`docs/diagrams/`](../diagrams/) — the publish HTTP/REST hop + gRPC fanout asymmetry from [RFC §C](0011-channels-bridges.md#c-message-routing-and-delivery) is the centerpiece.
- Concurrent-publish ordering disclaimer (per [OQ #5](0011-channels-bridges.md#open-questions)) called out in the user guide; missed-message recovery contract (on-startup last-N fetch per [OQ #8](0011-channels-bridges.md#open-questions)) documented alongside.

#### Tests

- Integration: `UserParticipant` joins a channel via REST and shows up in `GET /api/v1/channels/{id}` member list with `respond_policy=when_mentioned` — covered by `internal/server/channel_handlers_test.go::TestChannels_AddMember_NoContent`.
- Integration: human-published message flows through the agent gate (no implicit mention-all) — single-agent reply on explicit `--mention <agent>`, zero replies otherwise. Unit-level coverage in `tests/unit/python/test_channel_message_runtime.py`; end-to-end coverage in [MT-CHANNEL-004](../manual-tests/MT-CHANNEL-004.md).
- Integration: `channel watch` polling — covered by `cli/src/commands/channel_tests.rs::watch_state_*` (unit) + [MT-CHANNEL-003](../manual-tests/MT-CHANNEL-003.md) (live).
- Manual: MT-CHANNEL-001 … MT-CHANNEL-006 executed against a local docker-compose deployment; results captured in PR description.
- Docs lint: `make validate` green; new guide passes the existing markdown link check.

#### PR checklist

- [x] All six manual tests have a dedicated file under `docs/manual-tests/` and exit-criteria checklist
- [x] Channels user guide reviewed against `docs/documentation-guide.md`
- [x] Architecture diagram update lands in this PR (not deferred) — [system-overview.md](../diagrams/system-overview.md) gains the `internal/channels` node, the `ReceiveChannelMessage` gRPC edge, the persona → REST publish edge, and the `channels.db` store
- [x] OQ #4 (`watch --json`), OQ #7 (human gate-bypass), OQ #8 (missed-message recovery) resolution wording surfaced in the user guide

---

### PR 8: `feature/v030-rfc0011-close` — Review Follow-Ups + Internal-Scope Close

**Depends on**: PR 7.
**Estimated size**: ~150–300 lines.

| File | Change |
|------|--------|
| `docs/rfcs/0011-channels-bridges.md` | Status → `⚠️ Partially Implemented` (external bridges deferred to v0.5.0). |
| `ROADMAP.md` | RFC 0011 rows → `⚠️ Partially Implemented (internal channels)` (RFC Master Index + v0.3.0 Component Matrix). |
| `docs/v0.3.0-plan.md` | Master Progress Overview row 6 → 🔀 PR open while this PR is in flight; flips to ✅ on merge. |
| `internal/channels/sqlite_pr8_test.go` (new) | NTH dispatch — tightened `TestSQLiteStore_Close_Idempotent` (true idempotency assertion), new `TestSQLiteStore_MaxOpenConnsPinnedToOne` invariant pin, new `TestSQLiteStore_PublishMessage_FKDisambiguation_ChannelDeletedConcurrently` regression for the round-2 "deleted concurrently" branch (sub-case 1), new `TestSQLiteStore_PublishMessage_FKDisambiguation_InvalidThreadID` regression for the round-2 `invalid thread_id` branch (sub-case 2). Co-located in a dedicated file to keep `sqlite_test.go` under the 500-line cap. |
| `internal/channels/sqlite_test.go` | Old loose-assertion `TestSQLiteStore_Close_Idempotent` removed (replaced by the tightened version in `sqlite_pr8_test.go`). |

CHANGELOG.md is **deferred to v0.3.0 release prep** (Phase 4 PR 3).

#### PR checklist

- [x] All deferred review findings addressed or downgraded (see NTH disposition below)
- [x] `make test` passes; `make lint` clean
- [x] [docs/rfcs/0011-channels-bridges.md](0011-channels-bridges.md) status → `⚠️ Partially Implemented` (external bridges deferred to v0.5.0)
- [x] [ROADMAP.md](../../ROADMAP.md) RFC 0011 rows → `⚠️ Partially Implemented (internal channels)` (RFC Master Index + v0.3.0 Component Matrix both flipped)
- [x] [docs/v0.3.0-plan.md](../v0.3.0-plan.md) Master Progress Overview row 6 → 🔀 PR open (flips to ✅ on merge)
- [x] PR #231 review SF-1 closed: `buildDSN()` rejects (or merges) paths containing `?`; regression test for `file:`-URI input — landed via [ISSUE-0049](../issues/ISSUE-0049-builddsn-drops-pragmas-on-file-uri-paths.md) ahead of PR 8
- [x] PR #231 review NTH items dispatched (see disposition below)

#### PR #231 review NTH disposition

| NTH item | Disposition |
|----------|-------------|
| `PRAGMA user_version` baseline | ✅ Landed in PR 2's SF-4 migration — `applySchema` now stamps `channelStoreSchemaVersion` (`internal/channels/sqlite_schema.go:112`) and `TestSQLiteStore_SchemaVersion_Stamped` pins the contract. |
| Soft byte cap on `msg.Content` | ✅ Landed via [ISSUE-0050](../issues/ISSUE-0050-publishmessage-content-byte-cap.md) ahead of PR 8 — `MaxMessageContentBytes = 16_384` + `ErrMessageContentTooLarge` sentinel mapped to HTTP 413. |
| FK-disambiguation "channel deleted concurrently" test | ✅ Landed in this PR — `TestSQLiteStore_PublishMessage_FKDisambiguation_ChannelDeletedConcurrently` opens a second `sql.DB` to the same file with `foreign_keys = OFF` to bypass the cascade, leaves an orphaned membership row, then publishes through the store and asserts `ErrChannelNotFound: <id> (deleted concurrently)`. The test seam the original NTH note flagged turned out to be a four-line second-handle open. Round-out: `TestSQLiteStore_PublishMessage_FKDisambiguation_InvalidThreadID` pins sub-case 2 of the same round-2 branch (`chCount > 0` + non-empty `ThreadID` → "invalid thread_id"). Sub-case 3 (`chCount > 0` + empty `ThreadID` → raw error, marked "unexpected") is not tested: it cannot be triggered without a contrived driver-level seam, and adding one would falsely imply the branch is reachable in production. |
| `TestSQLiteStore_Close_Idempotent` rename or tightened assertion | ✅ Landed in this PR — name kept (the test *is* idempotent now), assertion tightened to `require.NoError` on both calls. The previous `_ = store.Close()` swallowed the second-call return; tightening pins `database/sql.DB`'s documented "Close is idempotent" contract so a future driver-side change that started returning `sql.ErrConnDone` on subsequent Close would red the test instead of silently changing the contract. |
| `db.Stats().MaxOpenConnections == 1` invariant test | ✅ Landed in this PR — `TestSQLiteStore_MaxOpenConnsPinnedToOne` red-flags the cap drift before a future contributor lifts the pool past 1 without first relaxing the txn shapes named in `NewSQLiteStore`'s rationale comment. |
| `BeforeConnect` hook for `foreign_keys = ON` paired with the PR 2 `MaxOpenConns` lift | ⏭ **Deferred to v0.4.0.** The hook is only required when `MaxOpenConns` exceeds 1; PR 2's "lift to 4–8" TODO (`internal/channels/sqlite.go:83`) was deliberately not exercised in v0.3.0 (no production REST traffic stresses it; the dmMu-protected `GetOrCreateDM` and the cap-check txn in `CreateChannel` both lean on serial-writer assumptions today). When v0.4.0 lifts the cap, the hook lands alongside that change so every connection in the pool starts with FK enforcement on, not just the first one — pinning a hook today against a single-connection pool would only test the DSN path that PR 1 already tested. Tracked alongside the `TODO(rfc0011-pr2)` comment in `sqlite.go`. |

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
- **PR 4 (action + servicer + gate + DELETE)**: largest persona-runtime touch + DELETE endpoints + integration test, plus the cross-cutting `EventType` / `ActionType` rename touching every persona-runtime call site. Revised 2026-05-04 to `~700–900 lines` after the [chat-as-DM amendment](0011-amendment-chat-as-dm.md) folded the RFC 0016 chat-path migration into PR 4a (atomic-rename requirement). The authoritative split is the [Scope split (PR 4a / PR 4b)](#scope-split-pr-4a--pr-4b) subsection above; this entry is retained only as the cap-overrun rationale for the pre-committed split. PR 5's joint-delivery dependency moves to 4b.
  - **Downstream impact** — [RFC 0020 PR plan](0020-pr-plan.md) PR 5's `Depends on: PR 4, RFC 0011 PR 4` row must be re-pinned to **RFC 0011 PR 4a**: RFC 0020 PR 5 needs the proto contract (event type, servicer, dispatcher), not the response gate. The split is now definitive (per the chat-as-DM amendment), so this re-pin is unblocked and should land in the same merge window as the amendment to remove cross-plan drift risk.

PRs 2, 3, 5, 6, 7 are within the calibrated band and are not pre-committing to a split.

## Cross-Plan Confirmations

- **Joint delivery** with [RFC 0020 PR plan](0020-pr-plan.md) PR 5 is reflected in both plans' PR 5 sections (this plan's PR 5 ↔ RFC 0020 PR 5). Either both merge in the same window or both ship the documented divergence path (per-event episodic writes here, backfill in v0.3.x).
- **Channel-ingest sanitization** is anticipated by [RFC 0009 PR plan](0009-pr-plan.md) PR 3 — both plans now point at the same integration site (PR 5 here, PR 3 there).
- **Memory facade tag/scope contract** is consumed at PR 5 from [RFC 0008 PR plan](0008-pr-plan.md) PR 2's frozen `compress`/`tags` API surface.
