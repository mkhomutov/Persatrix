# RFC 0042 — PR Implementation Plan (Phase 1 — vocabulary + wrappers)

> Owning RFC: [0042-state-namespacing-by-scope.md](0042-state-namespacing-by-scope.md) · Target: **v0.4.0+** (no version-plan doc exists yet — this plan is authored ahead of the v0.4.0 plan and slots into it when that lands). Reciprocal gate: RFC 0041's Phase-2 sweep re-types [`StateDelta.scope`](0041-typed-event-taxonomy-lifecycle-callbacks.md#f-backwards-compatibility) to the `Scope` enum PR 1 ships.

## Overview

RFC 0042 Phase 1 is **vocabulary + wrappers**: ship the `Scope` enum, the key parser/composition, the `ScopedState` API, and the per-scope wrapper routing over today's stores — **with no user-visible behaviour change and no call-site migration** (that is Phase 2). Phase 1 splits along a hard dependency line the RFC draws in [§Phased Implementation Plan](0042-state-namespacing-by-scope.md#phased-implementation-plan):

- **Phase 1a (PRs 1–5)** — the enum, API, wrappers, `channel_state` + `session_state` tables, and secret-lint. **Depends on nothing from RFC 0041** and is the critical path; it can be scheduled today.
- **Phase 1b (PR 6)** — `StateDelta` emission on every mutation. The **only** deliverable gated on [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1 landing the `StateDelta` event *shape* (RFC 0041 is currently only **Proposed**, so this is two status transitions away).

The design premise is that this RFC **rides the already-shipped scope-axis model**, it does not reinvent it: `agents/request_scope.py` binds `session` / `principal` / `epoch` per request, and the `MemoryStore` facade partitions rows by `(agent, principal, session, epoch)` ([memory-scope-axes.md](../memory-scope-axes.md), [RFC 0042 §G](0042-state-namespacing-by-scope.md#g-reconciliation-with-the-shipped-scope-model)). Every wrapper here **consumes** those resolved ids from the existing ContextVars rather than re-parsing them out of key strings.

This plan slices Phase 1 into small, independently-reviewable, test-first PRs. Each PR is green on `make test-python` (or `make test-go` for the Go slice), `make lint-python` (root ruff + mypy), and `python scripts/checks/file_size.py --strict` before merge, and keeps ROADMAP/RFC status hygiene.

### Open-question dispositions at plan-authoring time

Per [RFC 0042 §Open Questions](0042-state-namespacing-by-scope.md#open-questions): OQ #1–#4 are **DECIDED** and land in Phase 1 as noted; OQ #5 is **DEFERRED** to Phase 3; OQ #6 is **still open in the RFC** (lean: envelope-only). This plan adopts the OQ #6 lean and freezes it at PR 1, but that disposition is not ratified until the RFC leaves Draft — see the [RFC 0042 leave-Draft checklist](0042-state-namespacing-by-scope.md#decision--next-steps) item 2.

- **OQ #1 (`channel:` storage layout) → PR 3.** Sidecar `channel_state(channel_id, key, value)` table — the next `channelStoreSchemaVersion` after v10, a **pure-additive** migration with no data movement (precedent: v6 `epoch_id`, v8 operator config, v9 membership intervals). One of **two** carve-outs from the "no schema changes" guarantee: PR 3 lands the symmetric `session_state(session_id, key, value)` table (v11→v12) for the `session:` scope in the same slice.
- **OQ #2 (`interaction:` backing store) → PR 4.** Re-attributed away from RFC 0034 (which is the per-*channel* conversation window, not an interaction-keyed store): `interaction:` is backed by RFC 0020's in-memory `InteractionTracker` plus the RFC 0029 Scratchpad tier, exposed as a new interaction-close-bounded KV surface.
- **OQ #3 (wallet scope) → no wallet work in Phase 1.** The wallet is server-side (`internal/wallet/`), keyed `global`/`per_workflow`/`per_agent` with no session dimension, so it is **not** migrated to `session:` and is not the multi-owner example. A `session:`-scoped read-through *view* of wallet spend, if wanted, is a Phase-2 client-side convenience proposed separately.
- **OQ #4 (`app:` write boundary) → PR 4.** The Go init writer is `cmd/orchestrator/main.go`; the Python seed is the `agents/optimization.py` / `agents/model_aliases.py` config load. Agent workers get a **read-only** `app:` view whose `set(scope=app, …)` raises.
- **OQ #6 (`(principal, epoch)` envelope wiring) → PR 1 (envelope-only, assumed pending ratification).** No scope carries `principal`/`epoch` in the composed key; both are inherited from the ambient ContextVars and preserved by the facade's existing partitioning. The RFC still lists this as open (lean: envelope-only); this plan adopts that lean and freezes it **before** PR 1 fixes the parser, so PR 1 must not merge until the RFC ratifies the disposition (its leave-Draft checklist item 2).
- **DEFERRED — Phase 3:** OQ #5 (event-subscriber scope-visibility filter). RFC 0041's stream redactor scrubs content uniformly and is explicitly "not a callback"; a per-subscriber scope filter is a **new mechanism** (possibly an RFC 0041 amendment), out of this plan.

### File-size constraints (cap = 500 per [`scripts/checks/file_size.py --strict`](../../scripts/checks/file_size.py))

| File | Lines now | Headroom | Routing |
|------|-----------|----------|---------|
| New `agents/scoped_state.py` | — | — | Enum + parser + `ScopedState` Protocol + wrappers. **Split into `scoped_state.py` + `scoped_state_backends.py`** if it nears the cap — the per-scope backends are the bulk. |
| [`internal/channels/sqlite_migrations.go`](../../internal/channels/sqlite_migrations.go) | 467 | 33 | Add two dispatch arms — `case 11:` (channel_state) and `case 12:` (session_state) — in `applyMigration` (the switch dispatches on the *target* version; `case 10:` already routes v9→v10). Following the established pattern (v8→v9 in `sqlite_membership_intervals_migration.go`, v9→v10 in `sqlite_messages_fts_migration.go`), each migration body lands in a **new sibling** file (`sqlite_channel_state_migration.go`, `sqlite_session_state_migration.go`), keeping this file near-neutral (two two-line arms). |
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) | 223 | ample | Bump `channelStoreSchemaVersion` to 12; add the `channel_state` + `session_state` DDL to the fresh-schema path. |
| [`internal/security/redactor.go`](../../internal/security/redactor.go) | 488 | 12 | Secret-name pattern **source** for PR 5. PR 5 **reads** it (generates a Python parity module); do not add logic here — if a new exported pattern slice is needed, keep it to a few lines or generate from the existing export. |
| [`agents/memory/store.py`](../../agents/memory/store.py) | 468 | 32 | Home of the `MemoryStore` class the `persona:` adapter binds onto — `agents/memory/facade.py` is only an 83-line re-export shim (re-exports `MemoryStore` + `budget_to_limit`), not the store surface. The adapter itself is **new code in `scoped_state.py`** (PR 4); touch `store.py` only if the typed surface must grow — headroom is tight. |
| [`agents/persona_runtime/memory_context.py`](../../agents/persona_runtime/memory_context.py) | 483 | 17 | **Phase 2** F-3 recall call site — near cap; needs an extraction before migration. Out of this plan; flagged for Phase 2. |
| [`agents/persona_runtime/conversation_window.py`](../../agents/persona_runtime/conversation_window.py) | 444 | 56 | **Phase 2** `interaction:` call site. Out of this plan. |

## Dependency Graph

```
RFC 0031 axes (session/principal/epoch, shipped)   RFC 0029 MemoryStore facade (shipped)   RFC 0020 InteractionTracker (shipped)
        └───────────────────────────────┬───────────────────────────────────────────────────┘
   PR 1: agents/scoped_state.py — Scope StrEnum + key parser/composition   ← no external deps  [OQ #6 envelope-only]
        │
   PR 2: ScopedState Protocol + temp: backend + JSON serialization + increment + ambient owner-resolution
        │
        │   ┌╌╌ PR 3 (Go, internal/channels): channel_state + session_state additive migrations + opaque KV stores  [OQ #1]
        │   ╎   parallel track — no dependency on PR 1 / PR 2; can land any time before PR 4
        ▼   ╎
   PR 4: per-scope wrapper routing (persona/channel/session/interaction/app) + cross-owner denial  [OQ #2, #3, #4]
        │   └╌ the channel: / session: routes read/write PR 3's channel_state / session_state stores
   PR 5: secret-name lint (genpatterns parity from internal/security/redactor.go)
        │  ── Phase 1a complete: unblocked, no RFC 0041 dependency ──
        │
   PR 6: StateDelta emission on every mutation   ◀── gated on RFC 0041 Phase 1 (StateDelta type)  [Phase 1b]

   Phase 2 (separate): call-site migration (memory_context / conversation_window / channel_publisher /
   wallet-client read-through / F-3 recall). Phase 3: RFC 0037 overlay + OQ #5 subscriber filter. NOT in this plan.
```

## PR Sequence

### PR 1: `feature/v040-rfc0042-scope-enum` — `Scope` enum + key parser

The dependency-free leaf: the closed six-scope enum and the `<scope>:<owner_id>:<dotted.key.path>` parser/composer, built test-first, Python-only. Adopts the OQ #6 lean (envelope-only: `principal`/`epoch` never appear in the composed key), which the RFC must ratify before this PR merges (see the PR checklist precondition).

#### Scope

| File | Change |
|------|--------|
| New `agents/scoped_state.py` (enum + parser only) | `Scope(StrEnum)` with exactly `app` / `persona` / `channel` / `session` / `interaction` / `temp` ([§D](0042-state-namespacing-by-scope.md#d-the-scopedstate-api)); `compose_key(scope, owner_id, path)` and `parse_key(key) -> (scope, owner_id, path)` implementing [§F](0042-state-namespacing-by-scope.md#f-key-composition) (owner-less double-colon for `app:`/`temp:`; multi-owner inner id stays in `path`). Module docstring records the disambiguation from the two other "scope" meanings (wallet budget granularity; RFC 0020 recall partition) and from Go `internal/state`. |
| [`ROADMAP.md`](../../ROADMAP.md) | Add/refresh the RFC 0042 Master-Index row → 🚧 Implementing (target v0.4.0+); `Last updated` refresh (concise). |

#### Tests

- New `tests/unit/python/test_scoped_state_keys.py` — `Scope` membership is exactly the closed set (guards against silent additions per Goal 1); `parse_key(compose_key(...))` round-trips for every scope including owner-less `temp::retry_count` / `app::llm.default_alias`; a multi-owner path (`session:S-a:speaker.ember-owl.turns_taken`) parses with the inner owner left in `path`; an unknown prefix and a missing double-colon on `app:`/`temp:` are rejected; **no** parse path accepts `principal`/`epoch` as a key segment (envelope-only, OQ #6).

#### PR checklist

- [ ] **Precondition:** RFC 0042 has ratified OQ #6 as envelope-only (leave-Draft checklist item 2) — this PR fixes the key grammar, so the disposition must be settled before the parser freezes.
- [ ] Test-first (red → green); `make test-python` green for the new suite.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] No new runtime dependency; `agents.scoped_state` imports no `agents` runtime (leaf).
- [ ] ROADMAP + RFC status hygiene (RFC 0042 → Implementing).

### PR 2: `feature/v040-rfc0042-protocol-temp` — `ScopedState` Protocol + `temp:` backend

`agents/scoped_state.py` grows the `ScopedState` Protocol and its simplest backend (`temp:`, an in-process dict cleared per turn), establishing the serialization, `increment`, and owner-resolution contracts against the one scope that needs no store.

#### Scope

| File | Change |
|------|--------|
| `agents/scoped_state.py` | `ScopedState` Protocol (`get`/`set`/`delete`/`increment`/`list_keys`, [§D](0042-state-namespacing-by-scope.md#d-the-scopedstate-api)); the `temp:` in-process backend keyed per agent-worker turn; the **JSON serialization contract** (accept `str`/`int`/`float`/`bool`/`None`/`list`/`dict`; non-serializable raises at `set`); atomic `increment`; the ambient owner-resolution helper (resolves the running persona / bound session / interaction id from the existing ContextVars, never a caller-supplied segment). |
| Turn-boundary hook | The `temp:` backend clears on `Control(turn_completed)` / `turn_aborted` (the RFC 0041 turn events). In Phase 1a — before StateDelta wiring — this hooks the existing turn-completion seam in the agent loop; PR 6 does not change it. |

#### Tests

- New `tests/unit/python/test_scoped_state_temp.py` — `get`/`set`/`delete` on `temp:`; JSON round-trip for each supported type; a non-serializable value (`object()`) raises at `set`; **`increment` convergence**: N concurrent tasks incrementing `temp::count` converge to N; `list_keys(TEMP, prefix)` scopes within `temp:` only; `temp:` is empty after the turn-completion hook fires; owner-resolution injects the running persona's own id and refuses a hand-supplied foreign owner.

#### PR checklist

- [ ] Test-first (red → green); new suite green.
- [ ] `make lint-python` + `file_size.py --strict` clean (split `scoped_state_backends.py` if nearing cap).
- [ ] Concurrency test is deterministic (drive via an explicit scheduler seam; no wall-clock sleeps).
- [ ] ROADMAP + RFC status hygiene.

### PR 3: `feature/v040-rfc0042-sidecar-state` — `channel_state` + `session_state` migrations + opaque KV stores (Go)

The storage changes for the whole RFC: two pure-additive sidecar tables — `channel_state` and `session_state` — each with an opaque key-value accessor. **Go-side, self-contained** — no scope-prefix parsing crosses the wire (the Go stores see opaque blobs; [§D cross-language note](0042-state-namespacing-by-scope.md#d-the-scopedstate-api)).

#### Scope

| File | Change |
|------|--------|
| New `internal/channels/sqlite_channel_state_migration.go` | `migrateV10ToV11` — `CREATE TABLE channel_state(channel_id TEXT, key TEXT, value TEXT, PRIMARY KEY(channel_id, key))`, forward-only, purely additive (a pre-v11 DB reads back byte-identical). Matches the sibling-file pattern (`sqlite_membership_intervals_migration.go`, `sqlite_messages_fts_migration.go`). |
| New `internal/channels/sqlite_session_state_migration.go` | `migrateV11ToV12` — `CREATE TABLE session_state(session_id TEXT, key TEXT, value TEXT, PRIMARY KEY(session_id, key))`, forward-only, purely additive. **FK-less by design**: the synthetic `legacy` session (and any not-yet-registered id) has no `sessions` row, so — like `session_bindings` — the table carries no `session_id` foreign key. Same opaque-KV shape as `channel_state`. |
| [`internal/channels/sqlite_migrations.go`](../../internal/channels/sqlite_migrations.go) | Two arms — `case 11: return migrateV10ToV11(db)` and `case 12: return migrateV11ToV12(db)` — in `applyMigration`; the switch keys on the *target* version, and `case 10:` already exists (routes v9→v10). |
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) | Bump `channelStoreSchemaVersion` → 12; register the `channel_state` + `session_state` DDL in the fresh-schema path. |
| `internal/channels/channel_state_store.go`, `internal/channels/session_state_store.go` (new) | `Get`/`Set`/`Delete`/`ListKeys(ownerID, prefix)` over `channel_state` / `session_state` — opaque `string` values, no interpretation. |

#### Tests

- New `internal/channels/sqlite_channel_state_migration_test.go` + `sqlite_session_state_migration_test.go` — a v10 fixture DB migrates to v11 then v12 with every pre-existing table byte-identical (the additive-migration bar, mirroring `sqlite_session_migration_test.go`); the fresh-schema path yields the same shape as the migrated path.
- New `internal/channels/channel_state_store_test.go` + `session_state_store_test.go` — `Set`/`Get`/`Delete` round-trip; `ListKeys` prefix scan; two channels (resp. two sessions) with the same key are independent.

#### PR checklist

- [ ] Test-first (red → green); `make test-go` green.
- [ ] Both migrations are forward-only and pure-additive (no data movement; documented in each migration doc-comment).
- [ ] `file_size.py --strict` clean (migration bodies in the new sibling files, not `sqlite_migrations.go`).
- [ ] ROADMAP + RFC status hygiene.

### PR 4: `feature/v040-rfc0042-wrappers` — per-scope wrapper routing

Wire the persisted scopes to their backing stores behind `ScopedState`, with **no call-site migration** — new code can use the API; existing code is untouched. Resolves OQ #2 (interaction), OQ #3 (no wallet migration), OQ #4 (app read-only).

#### Scope

| File | Change |
|------|--------|
| `agents/scoped_state.py` (+ `scoped_state_backends.py` if split) | `persona:` → a KV adapter over the typed [`MemoryStore`](../../agents/memory/store.py) facade (trust-style keys map onto the typed bond methods; free-form persona keys use the adapter — the facade has no `get`/`set`, so this is new code, [§E](0042-state-namespacing-by-scope.md#e-mapping-to-existing-stores)); `channel:` → the PR 3 `channel_state` Go store via the existing channel client; `session:` → the PR 3 `session_state` Go store via the same client, keyed by the resolved `session_id` (no new store stood up here — PR 3 builds it, exactly parallel to `channel:`); `interaction:` → an interaction-close-bounded in-memory dict keyed by the RFC 0020 `InteractionTracker` scope, snapshot-able to the RFC 0029 Scratchpad tier; `app:` → a **read-only** read-through view (`internal/defaults/`, `config/*.yaml`, `agents/optimization.py` + `agents/model_aliases.py`); `set(scope=app, …)` raises. |

#### Tests

- New `tests/unit/python/test_scoped_state_routing.py` — every `(scope, owner_id, key)` tuple resolves to exactly one store, deterministically; the **same** dotted path in two scopes is independent; a `persona:B:*` read while running as persona A is **denied** (cross-owner bar, [Security](0042-state-namespacing-by-scope.md#security-considerations)); a serialization round-trip through each **persistent** backend (`persona`/`channel`/`session`); `set(scope=app, …)` raises; `list_keys` never crosses scopes.
- New `tests/integration/scope_routing_test.go` — the Go `channel:` and `session:` paths store/read opaque blobs written via the Python wrapper (end-to-end through the channel client).

#### PR checklist

- [ ] Test-first (red → green); `make test-python` + `make test-integration` green.
- [ ] No existing call site changed (Phase 2 is separate); existing `agents` suites pass unchanged.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] ROADMAP + RFC status hygiene.

### PR 5: `feature/v040-rfc0042-secret-lint` — secret-name lint (hard CI error)

A static check that flags any `set(scope=persona|channel|session|interaction, …)` whose key matches a known secret-name pattern — a hard CI error, not a warning ([Security](0042-state-namespacing-by-scope.md#security-considerations)). The pattern set is generated from the Go canonical source, not hand-copied.

#### Scope

| File | Change |
|------|--------|
| Pattern generation | Extend the [`cmd/genpatterns`](../../cmd/genpatterns) → Python parity mechanism to emit a **secret-name** pattern module from [`internal/security/redactor.go`](../../internal/security/redactor.go) `SecretRedactor` default patterns (RFC 0009). **Not** `agents/security_patterns.py`, which holds prompt-injection patterns. Add a `make` regen + `-check` target mirroring `generate-sanitizer-patterns`. |
| New `scripts/checks/scoped_state_secret_lint.py` (or a ruff/AST check) | Walk agent call sites for `set(scope=…, key=…)` where scope is persisted and `key` matches a generated secret-name pattern; fail the build. Wired into `make lint-python` / pre-commit. |

#### Tests

- New `tests/unit/python/test_scoped_state_secret_lint.py` — a secret-named key (`api_key`, `password`, `token`) under a persistent scope fails the lint; the same key under `temp:` is allowed; the lint reports file+line.
- New parity assertion (extend [`tests/unit/python/test_pattern_parity.py`](../../tests/unit/python/test_pattern_parity.py)) — the generated Python secret-name patterns match the Go source (regen is not stale).

#### PR checklist

- [ ] Test-first (red → green); new suites green; `make <secret-patterns>-check` green.
- [ ] Generated module is not hand-edited (regen gate enforces parity).
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] ROADMAP + RFC status hygiene. **Phase 1a complete** — the RFC's unblocked critical path is fully landed with no RFC 0041 dependency.

### PR 6: `feature/v040-rfc0042-statedelta-emit` — `StateDelta` emission (Phase 1b, gated on RFC 0041)

The only Phase-1 deliverable gated on RFC 0041: emit a `StateDelta` on every mutation. It needs only the `StateDelta` type's existence — `Scope` is a `StrEnum` whose value feeds RFC 0041 Phase 1's opaque `str` field directly; the Phase-2 re-typing is RFC 0041's follow-up ([RFC 0041 §F](0041-typed-event-taxonomy-lifecycle-callbacks.md#f-backwards-compatibility)).

#### Scope

| File | Change |
|------|--------|
| `agents/scoped_state.py` | Each mutating op (`set` / `delete` / `increment`) emits exactly one `StateDelta(scope=<Scope value>, …)` via the RFC 0041 dispatcher/stream; reads emit nothing. |
| RFC 0041 cross-ref | Note RFC 0041's Phase-2 sweep re-types `StateDelta.scope` from `str` to this `Scope` enum (the reciprocal gate). |
| RFC 0042 front-matter + this plan | On Phase-1 completion, `status: implementing → partially_implemented` (Phase 2/3 remain); check off the Phase-1a/1b slices. |

#### Tests

- New `tests/unit/python/test_scoped_state_events.py` — `set`/`delete`/`increment` each emit exactly one `StateDelta` carrying the correct scope value and key; `get`/`list_keys` emit nothing; the emitted `scope` string equals the `Scope` member value (the field RFC 0041 Phase 1 types as `str`).

#### PR checklist

- [ ] **Precondition:** RFC 0041 Phase 1 landed (the `StateDelta` event type is importable). Do not open until then.
- [ ] Test-first (red → green); new suite green.
- [ ] `make lint-python` + `file_size.py --strict` clean.
- [ ] ROADMAP + RFC status hygiene (RFC 0042 Phase 1 → Partially Implemented).

## Notes

- **Phase 2 is out of this plan.** Call-site migration — persona runtime (`memory_context.py`, `conversation_window.py`), channel publish (`channel_publisher.py`), the wallet-client `session:` read-through *view*, and the F-3 recall filter (`agents/memory/scope_recall.py`, `_session_filter.py`, `_principal_filter.py`, `_epoch_filter.py`) — moves those inline call sites onto `ScopedState` and removes legacy direct access. `memory_context.py` (483) and `conversation_window.py` (444) are near the cap and need pure-move extractions **before** any migration line lands (matching the established `action_validation.py` split pattern). None of this is in Phase 1.
- **Phase 3 is out of this plan.** The RFC 0037 channel-classification overlay verification, and OQ #5's event-subscriber scope-visibility filter (a new dispatcher-middleware-or-per-subscriber mechanism, possibly an RFC 0041 amendment — **not** borrowable from RFC 0041's content redactor). OQ #5 must resolve before Phase 3.
- **The wallet is not migrated.** RFC 0023's wallet is server-side (`internal/wallet/`) with `global`/`per_workflow`/`per_agent` budget scopes and no session dimension; `agents/wallet_client.py` is a stateless gRPC client. Phase 1 touches neither. A `session:`-scoped read-through *view* of wallet spend, if ever wanted, is a Phase-2 client-side convenience proposed on its own, not a state migration ([RFC 0042 §F](0042-state-namespacing-by-scope.md#f-key-composition)).
- **`Scope` stays Python-only in Phase 1.** The Go `channel_state` and `session_state` stores (PR 3) hold opaque blobs and never parse the prefix. If a Phase-2 Go component must recognize scope prefixes, `Scope` is first promoted to a generated cross-language closed set — a `proto/task.proto` enum or the `cmd/genpatterns` parity gate — never a hand-copied literal, the same discipline RFC 0041 applies to `StateDelta.scope`.
- **`internal/state` is unrelated.** The Go `internal/state` package (workflow-run execution state — `WorkflowRun`/`RunStatus`/`StepState`) is a name collision only; the Python module is `agents/scoped_state.py` (matching the `ScopedState` class and its tests) precisely to avoid overloading "state" a third time.
