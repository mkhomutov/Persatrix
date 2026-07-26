# RFC 0036 — PR Implementation Plan (Phases 1–3 — v0.3.9 scope)

**RFC**: [0036-persona-message-recall.md](0036-persona-message-recall.md)
**Created**: 2026-06-17
**Branch prefix**: `feature/v039-rfc0036-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.9-plan.md Phase 1 — workstream 1b (RFC 0036 PR plan — row 1b)](../v0.3.9-plan.md#phase-1--implement-the-ledger--recall)

---

## Overview

RFC 0036 gives a persona a `recall_channel_messages` tool that searches the **verbatim text** of past conversations — the "what was literally said" capability sitting beside the episodic *summary* tier (RFC 0005 / 0008). The search runs **server-side in the channel store**, joining a new FTS5 index over `messages` against the [RFC 0035](0035-channel-membership-interval-ledger.md) `membership_intervals` ledger, so a persona can only ever recall messages from channels it is or was a member of, restricted to its membership stints. A persona added → removed → re-added recalls **both** stints and neither the pre-join period nor the removal gap. The same membership filter is retrofitted onto the RFC 0034 conversation window so the persona's *live* prompt obeys the same scope.

The work splits into **6 PRs** across the RFC's three phases:

- **Phase 1 — server-side (PR 1–3).** PR 1 lands the dormant FTS index (migration v10). PR 2 is the **load-bearing scoping PR**: the scoped search query whose `membership_intervals` `EXISTS` clause *is* the access-control decision, plus the `epoch_id` hard filter (the [§OQ-6 lock](#open-question-resolutions-locked-at-plan-authoring-time)). PR 3 exposes it as `POST …/recall` with server-side audit. Testable end-to-end via REST with no persona involvement.
- **Phase 2 — the persona tool (PR 4).** `create_recall_tool` + `HttpRecallClient` (closure-bound `agent_id`), the new `channels:recall` permission, and per-row §F sanitization.
- **Phase 3 — the window filter (PR 5).** `?as_participant` on the history endpoint + the scoped history query; thread the persona id through the conversation window and catch-up. **Independent of Phases 1–2** — it needs only RFC 0035 and can land in parallel with PR 1–4.
- **PR 6** — review follow-ups + closeout.

**Hard prerequisite**: [RFC 0035 PR plan](0035-pr-plan.md) **PR 3 merged** — the `membership_intervals` table *and* its transactional write hooks must exist before any scoping query here can join them. The channel-store migration ordering enforces it: RFC 0035 takes **v9**, this RFC takes **v10**, and v9 must merge before v10 is authored ([v0.3.9-plan §Migration-slot note](../v0.3.9-plan.md#open-question-status)). Also depends on RFC 0034 Phase 2 (✅ v0.3.7 — the conversation window §G retrofits and the `_format_event` sanitizer §F reuses) and the channel store at v8 to take v9/v10 (✅).

### Open-question resolutions locked at plan-authoring time

All six RFC open questions resolve here; the [v0.3.9-plan §Open-question status](../v0.3.9-plan.md#open-question-status) table is the authority and the RFC's [Open Questions](0036-persona-message-recall.md#open-questions) mirror them.

- **[OQ #6](0036-persona-message-recall.md#open-questions) — epoch / session scoping (the load-bearing lock).** Recall **MUST** add `AND m.epoch_id = ?` bound to the caller's epoch — a **non-optional correctness requirement**, not an option: omitting it lets a persona recall a different run's or a post-`reset` epoch's messages (an isolation breach). `session_id` is **not** filtered — verbatim recall's value is cross-conversation and the membership interval is the intended access boundary, so recall spans a persona's whole history in a channel *within its epoch*. This sets the §C search predicate (PR 2) and the §G `as_participant` history clause (PR 5) **together** — both filter `epoch_id`, neither filters `session_id`. **Test obligation (both PRs)**: a different-epoch / post-`reset` message is never returned; two messages in the same channel + epoch but different sessions are *both* recallable (subject to the membership filter).
- **[OQ #1](0036-persona-message-recall.md#open-questions) — recall-endpoint authorization.** The endpoint and `as_participant` scope by a caller-supplied participant id on the currently-unauthenticated channel REST surface (single-tenant trust level). It MUST NOT ship more permissively than its neighbours and inherits RFC 0009's auth model when that lands; until then it matches the existing surface and **audits every call**. Does **not** block Phase 1 (the scope join is correct regardless of caller); blocks treating recall as safe multi-tenant.
- **[OQ #2](0036-persona-message-recall.md#open-questions) — permission granularity.** Ship a **distinct `channels:recall` permission**, not a reuse of `memory:read` — verbatim cross-channel recall is materially more sensitive than reading the persona's own summaries, so an operator can enable episodic recall while leaving verbatim recall off.
- **[OQ #3](0036-persona-message-recall.md#open-questions) — ranking blend.** Ship a BM25-dominant default with recency as a mild tiebreak; retune when recall-usage data exists, not on a schedule.
- **[OQ #4](0036-persona-message-recall.md#open-questions) — window-filter default.** `as_participant` filtering is **on** for persona-runtime callers (window + catch-up), **omitted** for human/CLI callers, with no operator opt-in — the window fix is the whole point of §G.
- **[OQ #5](0036-persona-message-recall.md#open-questions) — recall vs. episodic cross-referencing.** Out of scope; a follow-up once both surfaces are in use.

### Sequencing

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 6**, with **PR 5 sequenced flexibly** any time after RFC 0035 PR 3 (in parallel with PR 1–4).

PR 1 lands the FTS index dormant (no query reads it yet). PR 2 builds the scoped query at the store level — testable without an endpoint, and the load-bearing scoping + epoch-filter correctness gate. PR 3 wraps it in the audited REST endpoint. PR 4 adds the persona tool and the permission. PR 5 (the window retrofit) depends only on RFC 0035 + a scoped history query — it does **not** need the FTS index or the recall endpoint, so it is genuinely independent and separately reviewable. PR 6 closes out.

### Files-size constraints (verified at plan authoring)

Three host files sit **at or one line under** the [`file_size.py --strict`](../../scripts/checks/file_size.py) 500-line code cap, so new code is routed around them:

- `agents/tools/builtin.py` = **500** (at cap) → the recall tool lives in a **new `agents/tools/recall.py`** (per RFC); the registration glue in `builtin.py` must be net-line-neutral or free a line.
- `agents/persona_runtime/conversation_window.py` = **500** (at cap) → PR 5's `as_participant` threading must free a line or extract; it cannot net-add.
- `internal/server/channel_handlers.go` = **494** → the recall endpoint goes in a **new `persona_recall_handlers.go`**; only the small `as_participant` edit touches `channel_handlers.go`.

---

## Dependency Graph

```
RFC 0035 PR 3 merged (membership_intervals table + write hooks)   ← HARD PREREQUISITE
   │
   ├── PR 1 (Migration v10: messages_fts + triggers + FTS5 probe + backfill; migration tests)
   │     ↓
   ├── PR 2 (sqlite_search.go: scoped query — membership EXISTS + epoch filter + ranking +
   │     │   MATCH escaping + LIKE fallback; RecallMessages store method; scope/epoch/session tests)
   │     ↓
   ├── PR 3 (POST /api/v1/personas/{id}/recall + req/resp types + server-side RFC 0009 audit)
   │     ↓
   └── PR 4 (Phase 2: agents/tools/recall.py + channels:recall permission + §F sanitization)
   │
   └── PR 5 (Phase 3: ?as_participant on history + scoped GetHistory; thread agent_id through
             conversation_window + catch-up)   ← needs only RFC 0035 PR 3; parallel to PR 1–4
   ↓
PR 6 (review follow-ups + Phases 1–3 closeout)
```

---

## PR Sequence

### PR 1: `feature/v039-rfc0036-fts-migration` — Channel-Store Migration v10 (`messages_fts`)

**Depends on**: RFC 0035 PR 3 merged (v9 in place; v10 is the next slot).
**Purpose**: Land the FTS5 index over `messages` as channel-store migration **v10** — dormant; no query reads it until PR 2.

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/sqlite_migrations.go`](../../internal/channels/sqlite_migrations.go) | `migrateV9ToV10` + `case 10:` arm. In one transaction: probe FTS5 availability; if available, `CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=rowid)`, the `messages_ai` / `messages_ad` / `messages_au` triggers, and the `INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')` backfill; record an FTS5-unavailable flag otherwise; `stampUserVersionTx(tx, 10)` last. Header comment mirrors the existing migrations and records the **`messages_au` trigger as defensive-symmetric** (`messages` is insert-and-cap-prune only; the update trigger is expected never to fire) and the **`VACUUM` rowid-stability caveat** ([RFC §B](0036-persona-message-recall.md#b-fts5-index-over-messages)). |
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) | Bump `channelStoreSchemaVersion` 9 → 10; extend the migration-history header. |
| `internal/channels/sqlite_fts_migration_test.go` (new) | Migration tests (below). |

#### Key implementation details

- **Mirror the episodic-tier external-content pattern** ([`agents/memory/migrations.py:369-389`](../../agents/memory/migrations.py#L369-L389) `episodes_fts`, and the `_fts5_available` probe at [L467-476](../../agents/memory/migrations.py#L467-L476)). `messages` is TEXT-keyed (`id TEXT PRIMARY KEY`), so `content_rowid` aliases its implicit `rowid` — *not* preserved across an explicit `VACUUM`. No channel-store code path runs `VACUUM` today, so the risk is latent; the migration header records that adding compaction later must be followed by a `('rebuild')`.
- **External-content tables start empty** — the `('rebuild')` backfill is mandatory, not optional.
- **FTS5-unavailable builds skip `messages_fts`** and set the flag PR 2's query reads to take the `LIKE` fallback path — the same degradation the episodic tier ships.

#### Tests

- v9 → v10 builds `messages_fts` and backfills it from existing `messages` (a pre-seeded message is `MATCH`-findable post-migration).
- The `messages_ad` delete trigger removes a row from the index on hard delete; an insert adds one.
- `user_version` stamped inside the migration transaction; idempotent on reopen (no duplicate triggers, no double-rebuild).
- FTS5-unavailable path: the migration completes, sets the flag, and creates no virtual table (simulate via the probe).

#### PR checklist

- [ ] `go test ./internal/channels/ -run 'FTS|Migration|UserVersion' -count=1` passes.
- [ ] `make test` (Go lane) green; `go vet` clean.
- [ ] `channelStoreSchemaVersion == 10`; migration-history header updated with the `messages_au`-dormant and `VACUUM` notes.
- [ ] No query reads `messages_fts` yet (PR 2); no endpoint (PR 3).
- [ ] [v0.3.9-plan row 1b](../v0.3.9-plan.md#master-progress-overview) → 🔄 In progress; RFC 0036 Master-Index note `📋 Proposed → 🚧 Implementing` (front-matter `status:` + `make rfcs`).

---

### PR 2: `feature/v039-rfc0036-scoped-search` — The Scoped Search Query (load-bearing)

**Depends on**: PR 1 merged; RFC 0035 PR 3 (the ledger to join).
**Purpose**: The membership-scoped, epoch-filtered search query — the access-control decision, in SQL, at the store level. Testable without any endpoint or persona.

#### Scope

| File | Change |
|------|--------|
| `internal/channels/sqlite_search.go` (new) | The scoped query (§C) + a `RecallMessages(ctx, params RecallParams) ([]ChannelMessage, error)` store method. The `membership_intervals` `EXISTS` clause is RFC 0035 §F's predicate as a join; **`AND m.epoch_id = ?`** is bound non-optionally (§OQ-6 lock); `session_id` is **not** filtered. Optional narrowing (`channel_id`, `sender`, `after`, `before`) each applied only when supplied. BM25 `rank` normalised into `[0,1]` (reuse the episodic tier's `_normalize_bm25` shape) blended with a mild recency tiebreak. `MATCH` query escaping reuses the episodic tier's `safe_query` handling. `LIKE` fallback (`m.content LIKE '%'||?||'%'`) when FTS5 is unavailable, scope/narrowing unchanged. `limit` clamped server-side to a hard maximum. |
| [`internal/channels/channels.go`](../../internal/channels/channels.go) + [`store.go`](../../internal/channels/store.go) | `RecallParams` struct in `channels.go` (beside the sibling `ChannelMessage` / `Member` types); `RecallMessages` added to the `ChannelStore` **interface in `store.go`**, not `channels.go` (the struct keeps the interface line short). |
| `internal/channels/sqlite_search_test.go` (new) | Unit tests (below). |

#### Key implementation details

- **The `EXISTS` clause is non-optional and structural** — the persona's `participant_id` is a bound parameter (supplied by the endpoint path in PR 3), never derived from the FTS query text. A message is returned only if it falls inside one of the persona's membership stints for its channel. FTS `MATCH` syntax in the query cannot alter this clause.
- **Epoch binding** — `RecallParams.EpochID` defaults to [`DefaultEpochID`](../../internal/channels/sqlite.go#L42) (`"live"`). PR 3 threaded the caller's epoch through `resolveEpochOverride` → `EpochOverrideFromContext` into `RecallParams.EpochID`. ~~so recall and publish agree on the epoch axis~~ — **that claim was wrong** ([ISSUE-0106](../issues/ISSUE-0106-recall-epoch-filter-decoupled-from-unpersisted-publish-epoch.md)): publish deliberately never persists a non-`live` epoch onto the row (ISSUE-0085), so recall's filter and publish's rail were decoupled and an explicit non-`live` epoch recalled nothing through the real path. **Resolved in direction (b) by RFC 0037 PR 5 (v0.3.12)**: separate runs never share a channel-store DB, so the `epoch_id` body override was removed from the endpoint (any presence is a pointed 400) and the store's strict-equality `live` filter remains as a vestigial guard — see the amended [§OQ-6](0036-persona-message-recall.md#open-questions). `messages.epoch_id` exists from migration v6.
- **One reusable predicate** — the `EXISTS` + `epoch_id` clause is factored so PR 5's scoped history query reuses the *same* SQL fragment, keeping the §C predicate and the §G clause provably identical.

#### Tests

Per [RFC §Test Strategy](0036-persona-message-recall.md#test-strategy) + the §OQ-6 legs this plan adds:

- **Scope**: against the RFC 0035 join → leave → rejoin fixture, a message inside stint 1, one inside stint 2, one before the first join, and one in the removal gap classify correctly — both stints recallable, pre-join and gap unreachable.
- **Epoch (load-bearing)**: a message in a *different* epoch / a post-`reset` epoch is **never** returned, even when it matches the query and the membership window.
- **Session-span**: two messages in the same channel + epoch but different `session_id` are **both** returned (subject to membership) — recall is not session-scoped.
- **Narrowing**: `channel_id` / `sender` / `after` / `before` each filter as expected and compose.
- **Ranking**: the more relevant FTS hit ranks first.
- **FTS / `LIKE` fallback**: when FTS5 is disabled the `LIKE` fallback applies the **identical scope** but a substring (not whole-token) text match — a single term matches a superstring token (`budget`→`budgets`) under `LIKE` yet not under FTS5, so the two row sets diverge while access can never widen.
- **Unicode**: a non-Latin (Cyrillic) term filters rather than sanitizing to a match-all dump (`\p{L}\p{N}` sanitizer preserves non-ASCII terms).
- **`MATCH` safety**: a query containing FTS5 operator syntax is escaped — it neither errors the statement nor escapes scope.
- **`limit`** is clamped to the server-side maximum regardless of the requested value.
- **Retention**: a cap-pruned / deleted message is absent from results (gone from `messages_fts` via the delete trigger).

#### PR checklist

- [ ] `go test ./internal/channels/ -run 'Recall|ScopedSearch|Epoch|Match' -count=1` passes.
- [ ] `make test` green; `go vet` + `-race` clean on the channels package.
- [ ] The epoch filter is present and tested as **non-optional** — a test asserts a cross-epoch message is unrecallable (not incidental coverage).
- [ ] The `EXISTS`+`epoch` predicate is a shared fragment PR 5 can reuse.
- [ ] No REST surface yet (PR 3).

---

### PR 3: `feature/v039-rfc0036-recall-endpoint` — `POST …/recall` + Server-Side Audit

**Depends on**: PR 2 merged.
**Purpose**: Expose `RecallMessages` over REST, scoped by the path participant id, with the RFC 0009 audit event emitted **server-side in the handler**.

#### Scope

| File | Change |
|------|--------|
| `internal/server/persona_recall_handlers.go` (new) | `POST /api/v1/personas/{participant_id}/recall`. Body: `{ query, channel_id?, sender?, after?, before?, limit?, epoch_id? }`. The scope participant is the **path segment**, bound into `RecallParams.ParticipantID` — never a body field. Resolve the caller's epoch as publish does — `ctx, err = s.resolveEpochOverride(ctx, body.EpochID)` returns an override-bearing context (not a string), then bind `RecallParams.EpochID = channels.EpochOverrideFromContext(ctx)` (defaults to `DefaultEpochID` when unset). Emit the audit event (below) **here**, recording persona, query, narrowing params, and result **count** (not content), before returning. `POST` (not `GET`) because it carries structured params + a free-text body and is audited — a command, not a cacheable fetch. A **new file** to keep clear of `channel_handlers.go`'s 494-line cap. |
| [`internal/server/channel_types.go`](../../internal/server/channel_types.go) | Recall request/response types (`{ messages: [{ message_id, channel_id, sender, timestamp, content }] }`). |
| [`internal/security/audit_event.go`](../../internal/security/audit_event.go) | New `AuditChannelRecall AuditEventType = "channel.recall"` constant; add it to `AllAuditEventTypes()` **and** classify it in the security/telemetry map (the closed-set `TestEveryAuditEventType_HasSeverityClassification` fails otherwise). |
| router wiring | Register the route next to the existing persona/channel routes. |
| `internal/server/persona_recall_handlers_test.go` (new) | Handler + audit tests (below). |

#### Key implementation details

- **Audit emitted at the endpoint, not the tool** — a bypassed or misbehaving tool client cannot suppress the trail, and the audited request is the one the server actually scoped and executed ([RFC §Security — Audit](0036-persona-message-recall.md#security-considerations)). The event records the result **count**, never the recalled content.
- **OQ #1 posture inline** — the handler matches the existing channel surface's (unauthenticated, single-tenant) trust level and documents that it inherits RFC 0009's auth model when that lands; until then the audit makes any misuse observable. Do not add bespoke auth.
- **`limit` clamp** is enforced in PR 2's store method; the handler passes the requested value through and lets the store clamp, so the bound holds even if a future caller bypasses the tool.

#### Tests

- End-to-end via REST: seed a channel, add → remove → re-add a participant, then `POST …/recall` returns both stints and excludes the gap.
- A cross-epoch message is not returned through the endpoint (epoch resolved from the request, defaulting to `live`).
- Every recall call emits exactly one `channel.recall` audit event with the persona, query, narrowing params, and result count — and **not** the content.
- `limit` above the maximum is clamped; a malformed body returns 400.

#### PR checklist

- [ ] `go test ./internal/server/ -run 'Recall|Audit' -count=1` passes; `go test ./internal/security/ -run 'AuditEventType' -count=1` green (closed-set classifier).
- [ ] `make test` green; route registered; `channel_handlers.go` untouched / ≤ 500 lines.
- [ ] Audit emitted server-side; content never logged; OQ #1 posture documented inline.
- [ ] End-to-end add/remove/re-add recall test green (the structural half of `MT-PERSONA-RECALL-001`).

---

### PR 4: `feature/v039-rfc0036-tool-and-permission` — Phase 2: Persona Tool + `channels:recall`

**Depends on**: PR 3 merged.
**Purpose**: The `recall_channel_messages` persona tool, the distinct `channels:recall` permission, and per-row §F sanitization.

#### Scope

| File | Change |
|------|--------|
| `agents/tools/recall.py` (new) | `create_recall_tool(http_client, gate, *, agent_id)` factory (closure-bound `agent_id`, exactly like [`create_memory_tools`](../../agents/tools/builtin.py#L333)) + a small `HttpRecallClient` modelled on [`HttpChannelHistoryFetcher`](../../agents/channel_history_fetcher.py) (shared `aiohttp` session + timeout conventions). The `@tool` carries `permissions=["channels:recall"]`, `tier="builtin"`; the body checks `gate.check("channels:recall")` first and returns a failed `ToolResult` on denial — mirroring the `memory:read` check in `recall_notes`. LLM supplies `query`, `channel_id`, `sender`, `limit`; it **cannot** supply or override `agent_id` (passed as the endpoint path segment by the closure). |
| [`agents/tools/builtin.py`](../../agents/tools/builtin.py) | Register the tool where the persona toolset is assembled. **At 500 lines (cap)** — the registration must be net-line-neutral or free a line; if neither is clean, move the assembly glue into `recall.py` and import it. |
| [`config/agents.yaml`](../../config/agents.yaml) | Add a `channels: { recall: true }` block under the relevant persona(s)' `permissions:` (deny-by-default — absent ⇒ tool unavailable). |
| [`schemas/agent.schema.json`](../../schemas/agent.schema.json) | Add a `channels` object (`additionalProperties: false`, `recall: { type: boolean }`) to the `permissions` definition — which is currently `additionalProperties: false`, so the new category must be declared explicitly. |
| §F sanitization | Each recalled `content` row passes through the `_format_event` `CHANNEL_MESSAGE` delimiter-escape ([`prompt_assembly.py:421-426`](../../agents/persona_runtime/prompt_assembly.py#L421-L426)) via the existing [`_format_peer_turn`](../../agents/persona_runtime/conversation_window.py#L405-L438) reuse — applied **per row** as the tool assembles its `data`, since the result returns as a `tool_result` block, not a user turn. Each row is tagged with origin `channel_id` + `sender` so the model knows it is quoting cross-context material. |
| `tests/unit/python/tools/test_recall.py` (new), `tests/integration/persona/test_message_recall.py` (new) | Tests (below). |

#### Key implementation details

- **`gate.check("channels:recall")`** maps to `permissions.channels.recall: true` via the dotted-string [`PermissionGate.check`](../../agents/tools/permissions.py) (`category:action`). Deny-by-default: a persona without the block gets a failed `ToolResult`, and existing configs without the block load unchanged.
- **Distinct from `memory:read`** (OQ #2) — an operator can enable episodic recall while leaving verbatim recall off.
- **Sanitization reuses, never re-implements** the RFC 0034 §D escape — a `<|user_message|>` literal must round-trip inert.
- **System-prompt guidance** notes recalled content is reference material, not a licence to rebroadcast (the residual rebroadcast risk §Security documents).

#### Tests

- `agent_id` is closure-bound — a tool argument cannot override the scope participant.
- A `channels:recall` denial returns a failed `ToolResult`.
- Recalled `content` is delimiter-escaped (`<|user_message|>` round-trips inert).
- Default call (`query` only) searches all accessible channels; a `channel_id` narrows it.
- `make validate` passes against the new schema block; a config without the block loads and the tool is simply unavailable.
- Integration: a persona answers a question that requires recalling a specific past message.

#### PR checklist

- [ ] `pytest tests/unit/python/tools/test_recall.py tests/integration/persona/test_message_recall.py -q` passes.
- [ ] `ruff check agents/` + `mypy agents/` clean; `make validate` green.
- [ ] `builtin.py` still ≤ 500 lines (registration net-neutral or glue moved).
- [ ] `channels:recall` is a distinct permission; deny-by-default verified; existing configs load unchanged (additive proof).
- [ ] Per-row sanitization verified by the round-trip test.
- [ ] RFC 0036 Master-Index note advanced; [v0.3.9-plan row 1b](../v0.3.9-plan.md#master-progress-overview) reaffirmed In progress.

---

### PR 5: `feature/v039-rfc0036-window-filter` — Phase 3: Conversation-Window Membership Filter

**Depends on**: RFC 0035 PR 3 only. **Independent of PR 1–4** — needs neither the FTS index nor the recall endpoint, so it may land in parallel with them, any time after the ledger's write hooks merge.
**Purpose**: Make the live persona prompt obey the same membership scope as recall — close the incoherence where a re-added persona could *see* gap messages live but could not *recall* them.

#### Scope

| File | Change |
|------|--------|
| [`internal/server/channel_handlers.go`](../../internal/server/channel_handlers.go#L347) | `handleGetChannelHistory` gains an optional `?as_participant=<id>` query param; when present, route to a scoped history query applying the **same `EXISTS` + `epoch_id` clause** (reuse PR 2's fragment, or — if PR 5 lands first — introduce it here and PR 2 reuses it). Keep the edit minimal — the file is at 494 lines; if the change would breach the cap, extract the scoped branch into a helper in a new file. Human/CLI callers omit `as_participant` and are unaffected. |
| [`internal/channels/sqlite_messages.go`](../../internal/channels/sqlite_messages.go#L248) | A `GetHistoryScoped(ctx, channelID, participantID, limit, before)` variant joining `membership_intervals` + filtering `epoch_id`; `GetHistory` unchanged for unscoped callers. |
| [`agents/channel_history_fetcher.py`](../../agents/channel_history_fetcher.py) | `fetch(channel_id, *, limit, as_participant: str \| None = None)` — append `&as_participant=` to the GET when set. Room under the cap (139 lines). |
| [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py#L263) | `_fetch_window` passes the persona's `agent_id` as `as_participant`; **the cache key must include `as_participant`** (today `(channel_id, limit)`) so a scoped fetch is never served from an unscoped entry. **At 500 lines (cap)** — free a line or extract; cannot net-add. |
| [`agents/channel_catchup.py`](../../agents/channel_catchup.py) | The boot-time replay passes `as_participant` so episodic seeding excludes pre-join / gap messages. |
| tests | Integration: a re-added persona's window and catch-up both exclude removal-gap messages; for a current single-stint member the filter is a no-op on recent messages. |

#### Key implementation details

- **No-op for current members** — a persona with one open interval sees the filter trim only the pre-join prefix and any removal gap; recent messages are unaffected, so the common case is unchanged ([RFC §G](0036-persona-message-recall.md#g-conversation-window-membership-filter)).
- **`epoch_id` filter applies here too** (§OQ-6 lock) — the §G clause and the §C predicate filter epoch together.
- **Cache-key correctness is load-bearing** — omitting `as_participant` from the key would let an unscoped (or differently-scoped) entry serve a wrong window; this is a correctness fix, not an optimization.
- **Human/CLI unaffected** — `as_participant` omitted ⇒ `GetHistory` path, byte-identical behaviour.

#### Tests

- A re-added persona's live window excludes gap messages and includes both stints' recent messages; catch-up seeds episodic memory without gap/pre-join messages.
- A current single-stint member's window is byte-identical to today (no-op proof).
- The window cache does not serve an unscoped entry to a scoped fetch (cache-key regression).
- Human/CLI history fetch (no `as_participant`) is unchanged.

#### PR checklist

- [x] `pytest` (window + catch-up + fetcher legs) passes; `go test ./internal/server/ ./internal/channels/ -run 'GetHistoryScoped|HistoryEndpoint' -count=1` green (`-race` clean).
- [x] `conversation_window.py` (500, at cap) and `channel_handlers.go` (494) still ≤ 500 lines; the new server routing lives in `channel_history_scoped.go` so the at-cap host files take only minimal edits (the PR-5 review docstring note used the line the `as_participant` threading had freed, leaving the window file back at the 500 cap).
- [x] Cache key includes `as_participant` (`(channel_id, limit, agent_id)`); the distinct-personas-don't-share-cache and no-op-for-current-member (store-level `TestGetHistoryScoped_CurrentMemberMatchesUnscopedTail`) proofs are green.
- [x] `ruff` + `mypy` clean; full Go channels/server suites green (the 5 `TestShellExec` failures are a pre-existing `python`-binary-on-PATH environment issue, unrelated to this PR).

---

### PR 6: `feature/v039-rfc0036-close` — Review Follow-Ups + Closeout

**Depends on**: PR 4 and PR 5 merged.
**Purpose**: Fold in PR 1–5 review findings (each paraphrased inline, never linking a local review report per [.github/copilot-instructions.md](../../.github/copilot-instructions.md)) and mark RFC 0036 implemented.

#### Scope

| File | Change |
|------|--------|
| (various) | `From PR N review` follow-ups, populated as PRs are reviewed. |
| [`docs/rfcs/0036-persona-message-recall.md`](0036-persona-message-recall.md) | Status → `✅ Implemented`; "Implemented in v0.3.9" note in Decision/Next Steps; `make rfcs` to regenerate [INDEX.md](INDEX.md). |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0036 Master-Index row → `✅ Implemented`; `Last updated` refresh. |
| [`CHANGELOG.md`](../../CHANGELOG.md) | Seed the `[0.3.9]` **Upgrade Notes** — incl. the RFC 0035 §D pre-ship backfill gap as a known recall limitation (stints that closed before migration v9 are unrecallable; [RFC 0035 §D](0035-channel-membership-interval-ledger.md#d-backfill) / [OQ #1](0035-channel-membership-interval-ledger.md#open-questions)). |
| [`docs/guides/persona-agents.md`](../guides/persona-agents.md), [`docs/diagrams/memory-architecture.md`](../diagrams/memory-architecture.md) | Document the recall tool and the verbatim-vs-summary distinction (also a v0.3.9 release-prep doc-sweep deliverable — coordinate with [v0.3.9-plan Phase 3](../v0.3.9-plan.md#phase-3--v039-release-prep-execution)). |
| [`docs/rfcs/0036-pr-plan.md`](0036-pr-plan.md) | [Progress Overview](#progress-overview) rows filled. |

#### PR checklist

- [x] All PR 1–5 review findings addressed inline or tracked with rationale. (Each was folded into its own PR at merge time — notably the RFC 0009 `<external_data>` envelope + the centralised `agents.prompt_safety` escape rode PR 4; no findings remained for this closeout to fold.)
- [x] `make test` + `make validate` + lint clean.
- [x] RFC 0036 status flipped; `make rfcs` regenerated `INDEX.md`.
- [x] `CHANGELOG.md` `[0.3.9]` **Upgrade Notes** seeded — incl. the RFC 0035 §D pre-ship backfill-gap recall caveat (the forward-reference RFC 0035's closeout parks here).
- [x] `MT-PERSONA-RECALL-001` authored (live execution is [v0.3.9-plan Phase 3](../v0.3.9-plan.md#phase-3--v039-release-prep-execution)).
- [x] ROADMAP + [v0.3.9-plan row 1b](../v0.3.9-plan.md#master-progress-overview) reflect the final state.
- [x] `docs/guides/persona-agents.md` + `docs/diagrams/memory-architecture.md` document the recall tool + the verbatim-vs-summary distinction.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| Recall **leaks across runs** if the `epoch_id` filter is forgotten. | The filter is **non-optional** on both the §C query (PR 2) and the §G clause (PR 5); acceptance requires an explicit cross-epoch-unrecallable test, not incidental coverage ([§OQ-6 lock](#open-question-resolutions-locked-at-plan-authoring-time)). |
| Cross-scope leakage — a persona recalls a channel/stint it was not in. | The `membership_intervals` `EXISTS` clause is server-side, in SQL, non-optional, and the `participant_id` is bound from the request path, never from LLM args. Its correctness rides RFC 0035's ledger correctness (reviewed as one surface). |
| Recalled verbatim text is a **larger prompt-injection surface** than a live message. | Per-row `_format_event` delimiter-escape (§F), reusing RFC 0034 §D; a `<|user_message|>` literal round-trips inert (tested); each row tagged with origin so the model knows it is quoting cross-context material. |
| The two migrations (v9 ledger, v10 FTS) land out of order, or the FTS index desyncs from `messages`. | The RFC 0035 → 0036 dependency forces v9 before v10 (this plan gates on RFC 0035 PR 3); the `messages_ad` delete trigger keeps the index consistent with hard deletes; the `VACUUM` caveat is recorded in the migration (no code runs `VACUUM`). |
| **Rebroadcast** — a persona lifts user A's words from channel X into channel Y, even within scope. | Cannot be fully prevented (recall *is* read access); mitigated by origin-tagging + system-prompt guidance. Residual risk documented and accepted ([RFC §Security](0036-persona-message-recall.md#security-considerations)). |
| Recall **re-introduces idle / unbounded cost**. | Recall is FTS5/`LIKE` SQL in the channel store — **no LLM call**, so it does not touch the wallet or the zero-idle-cost invariant; `limit` is clamped server-side; the only write amplification is one index-insert per published message. |
| **File-size cap** breaches in `builtin.py` (500), `conversation_window.py` (500), `channel_handlers.go` (494). | New code routed to new files (`recall.py`, `sqlite_search.go`, `persona_recall_handlers.go`); the at-cap edits (tool registration, `as_participant` threading) must net-zero or extract — each PR checklist re-verifies the cap. |
| The recall endpoint ships on the unauthenticated channel surface (OQ #1). | It matches its neighbours' trust level, audits every call (observable misuse), and inherits RFC 0009's auth when it lands. Does not block Phase 1; blocks treating recall as multi-tenant-safe. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md) and [v0.3.9-plan §ROADMAP hygiene](../v0.3.9-plan.md#roadmap-hygiene):

- **PR 1 opens** → RFC 0036 Master-Index note `📋 Proposed → 🚧 Implementing` (front-matter `status:` + `make rfcs`); [v0.3.9-plan row 1b](../v0.3.9-plan.md#master-progress-overview) → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview) row; `Last updated` refresh on each flip; seed the CHANGELOG `[0.3.9]` entry once the persona tool (PR 4) lands.
- **PR 6 merges** → RFC 0036 → `✅ Implemented`; row 1b → ✅; `Last updated` refresh; docs updated.

---

## Progress Overview

| # | Phase | Title | Branch | Status | GitHub PR | Merged |
|---|-------|-------|--------|--------|-----------|--------|
| 1 | 1 | Migration v10 — `messages_fts` + triggers + backfill | `feature/v039-rfc0036-fts-migration` | ✅ Merged | [#675](https://github.com/mkhomutov/Persatrix/pull/675) | `5cd5b1e` |
| 2 | 1 | Scoped search query (membership EXISTS + epoch filter) | `feature/v039-rfc0036-scoped-search` | ✅ Merged | [#676](https://github.com/mkhomutov/Persatrix/pull/676) | `07f79f1` |
| 3 | 1 | `POST …/recall` endpoint + server-side audit | `feature/v039-rfc0036-recall-endpoint` | ✅ Merged | [#677](https://github.com/mkhomutov/Persatrix/pull/677) | `55dfbf5` |
| 4 | 2 | Persona tool + `channels:recall` + §F sanitization | `feature/v039-rfc0036-tool-and-permission` | ✅ Merged | [#678](https://github.com/mkhomutov/Persatrix/pull/678) | `fe14f50` |
| 5 | 3 | Conversation-window membership filter (independent) | `feature/v039-rfc0036-window-filter` | ✅ Merged | [#679](https://github.com/mkhomutov/Persatrix/pull/679) | `3132d7b` |
| 6 | — | Review follow-ups + closeout | `feature/v039-rfc0036-close` | 🔀 PR open | _this PR_ | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [RFC 0036 — Persona Verbatim Message Recall](0036-persona-message-recall.md) — canonical spec; §B FTS index, §C scoped query, §F sanitization, §G window filter, §Security audit.
- [RFC 0035 — Channel Membership Interval Ledger](0035-channel-membership-interval-ledger.md) / [RFC 0035 PR plan](0035-pr-plan.md) — the hard dependency; this plan gates on its PR 3.
- [v0.3.9-plan.md](../v0.3.9-plan.md) — master plan (row 1b is this workstream); locks the §OQ-6 epoch-filter / session-span decision and Phase-3-IN.
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md) / [RFC 0034 PR plan](0034-pr-plan.md) — the conversation window §G retrofits; the `_format_event` sanitizer §F reuses.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the durable `messages` store and the REST channel surface.
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — the audit subsystem recall emits to; the auth model OQ #1 defers to.
- [RFC 0005](0005-persona-agent-memory.md) / [RFC 0008](0008-agent-memory-context-optimization.md) — the episodic *summary* tier recall is the verbatim sibling of.
- [BRANCHING.md](../BRANCHING.md) — squash-merge + file-size-cap conventions.
