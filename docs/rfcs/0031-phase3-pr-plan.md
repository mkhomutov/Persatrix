# RFC 0031 — PR Implementation Plan (Phase 3 — Operator CLI + Active-Session Resolution)

**RFC**: [0031-per-session-namespacing-channels.md](0031-per-session-namespacing-channels.md)
**Status**: ✅ Implemented — v0.3.5 (Phase 2 of [v0.3.5-plan.md](../v0.3.5-plan.md)); all five PRs merged 2026-05-30 · see [Amendment — scope-axes reframing](#amendment--scope-axes-reframing)
**Created**: 2026-05-29
**Branch prefix**: `feature/v035-rfc0031p3-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Companion to**: [0031-phase2-pr-plan.md](0031-phase2-pr-plan.md) (Phase 2 — recall filtering, v0.3.5, ✅ Implemented) · [0031-pr-plan.md](0031-pr-plan.md) (Phase 1 — storage/write-path, v0.3.1, shipped)

---

## Overview

RFC 0031 Phase 2 ([0031-phase2-pr-plan.md](0031-phase2-pr-plan.md)) closed F-3: default recall is now session-scoped across all four persona-memory tiers, with the `legacy` carve-out keeping pre-RFC rows always visible. Two correctness gaps that recall filtering exposed in the multi-persona process were closed in the same release — the process-global session id ([ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md)) and the orchestrator never emitting a per-request session ([ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) Part 1). After all of that, **a session is still set only by the `PERSATRIX_SESSION_ID` env var or auto-minted by the orchestrator per `(agent, channel, user)`**; an operator has no first-class verb to create, name, list, switch, or archive one.

**Phase 3 ships the [RFC §E](0031-per-session-namespacing-channels.md#e-operator-surface) operator surface.** It is the third of the three resolution mechanisms the RFC phases (env var → file → flag); after this phase, all three are wired, which is the precondition Phase 4 waits on before publishing `docs/guides/sessions.md` (so the guide never documents a setting that does not work yet — [RFC §E phasing note](0031-per-session-namespacing-channels.md#e-operator-surface)).

Phase 3 has **no PR plan today** — the [v0.3.5 master plan §Phase 2](../v0.3.5-plan.md#phase-2--author--implement-rfc-0031-phase-3-operator-cli) names authoring this document as the deliverable, then executing it. This plan splits Phase 3 into **5 PRs**: one orchestrator REST surface, three Rust-CLI PRs (registry verbs → active-session pointer file → `--session` override), and a closeout.

### Scope correction surfaced during planning research

Two assumptions baked into RFC §E (authored 2026-05-12, before the ISSUE-0081/0082 session-model amendments landed) need reconciling before the CLI is built. Both are recorded in [§The session model the CLI sits on top of](#the-session-model-the-cli-sits-on-top-of) and consumed by the PRs below:

1. **§E assumes the active-session file is read by the orchestrator at startup** to set "the" process session. That is still true for the boot seed, but it is no longer the whole story: post-[ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md), the orchestrator **auto-mints a per-request session per `(agent, channel, user)`** on the live channel-dispatch path and emits it as the `persatrix-session` gRPC header, so the boot session governs only the paths that have no per-request binding (CLI chat, single-conversation deployments). The operator CLI's job is therefore **inspection + lifecycle + per-invocation override**, not "set the one global session." This plan makes that explicit so PR 4 wires `--session` as an *override above the auto-binding*, not as a setter of a single global.

2. **The Rust CLI is a thin REST client** ([`cli/src/main.rs`](../../cli/src/main.rs) — `reqwest` against the orchestrator at `:8080`; no SQLite, no gRPC, no `~/.persatrix/` handling today). The `sessions` registry lives in the orchestrator-owned `channels.db` ([`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go), RFC 0031 Phase 1 migration v3; also populated by the ISSUE-0082 PR 1 auto-mint path). So `session new / list / archive` **cannot** read or write the registry directly — they need an orchestrator-side `/api/v1/sessions` REST surface, which **does not exist yet** (PR 1 adds it). Only the active-session *pointer file* and the precedence resolution are genuinely CLI-local (PRs 3–4).

### `persatrix memory recall --all-sessions` is carved out, not built here

The [v0.3.5 master plan §Phase 2 acceptance](../v0.3.5-plan.md#phase-2--author--implement-rfc-0031-phase-3-operator-cli) and [RFC §Security Considerations](0031-per-session-namespacing-channels.md#security-considerations) mention a `persatrix memory recall --all-sessions` verb as the only operator route to the `sessions="*"` debug mode. Planning research found **no `persatrix memory` command, no memory-recall REST endpoint, and no recall RPC** anywhere today — surfacing `"*"` to an operator means building an entire operator memory-inspection surface (CLI verb + orchestrator REST + a gRPC recall path into each persona's `memory.db`), which is a *different* story from the session operator surface and is conspicuously absent from [RFC §E's own Phase 3 deliverable list](0031-per-session-namespacing-channels.md#phase-3-operator-cli). This plan **carves it out as a follow-up issue** (mirroring how Phase 4 carves out `persatrix memory legacy-prune`). Keeping it out has a security upside: the `"*"` sentinel keeps **no operator entry point at all**, so it provably cannot reach a prompt context — strictly stronger than the Phase 2 guarantee. See [§Open-question status — OQ #6 amendment](#open-question-status) and [PR 5](#pr-5-featurev035-rfc0031p3-close--closeout--all-sessions-carve-out). *(Decision flagged for the maintainer in the PR thread.)*

---

## Amendment — scope-axes reframing

**Recorded 2026-05-30, after Phase 2 shipped.** A design review of RFC 0031 against two edge cases — a channel with **no human participant**, and a room with **multiple humans + multiple agents** — found that `session_id` was overloaded across four jobs and that the `(recipient-agent, channel, sender)` binding *fragments* a multi-party room (one agent gets a separate session per speaker). The resolution reframes the model into four orthogonal axes; full model, glossary, and decision record in [Memory Scope Axes](../memory-scope-axes.md), recorded on the RFC in the [§A amendment](0031-per-session-namespacing-channels.md#a-vocabulary). Parts of this plan predate it:

**What the reframing changes under this plan:**

- **Session unit drops the sender axis** — the `(agent, channel, user)` binding referenced throughout this plan ([Overview](#overview), [the session-model table](#the-session-model-the-cli-sits-on-top-of), PR 4, PR 5) is superseded by **`(agent, channel)`**. Two DM threads are already distinct channel ids, so the channel axis alone isolates them; the sender axis only ever changed the group case, and changed it wrongly. This was a code change to the shipped [ISSUE-0082 binding](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) (`internal/channels/session_binding.go`), **upstream of this CLI work — not one of these five PRs**. ✅ **[ISSUE-0083](../issues/ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md) shipped 2026-05-30** (channel-store schema v5), **before PR 4**, so PR 4 wires the override against the final `(agent, channel)` key.
- **"Session" is now room-continuity, not a run-isolation namespace.** F-3 run-isolation moves to a new **epoch** axis (orthogonal column, modeled on `principal_id`, no `legacy` carve-out). The operator's "give me a fresh world for this test run" need — which this plan implicitly served via `session new --activate` — is now an *epoch* concern, so `session new --activate` activates a continuity room, it does **not** hand back a clean slate.

**Impact on the five PRs:**

- **PR 1–3 (REST registry surface, registry verbs, pointer file): unaffected.** They manage the `sessions` *registry* and the active-session *pointer*, both independent of the binding key. They ship as written.
- **PR 4 (`--session` override): unaffected in shape.** It still overrides the per-request binding for one invocation; only the binding's key changes (upstream). Its inline `(agent, channel, user)` references should read `(agent, channel)`.
- **PR 5 (closeout integration test):** the canonical "two senders → distinct sessions" assertion lives in `grpc_dispatcher_session_test.go` and is inverted by [ISSUE-0083](../issues/ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md), **not** by PR 5's operator-surface test (which, as scoped, exercises override-vs-auto-binding, not multi-party recall). If PR 5 adds a multi-party recall leg, it must assert the post-reframing "two senders in one room → one shared room session" and be sequenced *after* the sender-axis drop so it never pins the pre-reframing behaviour.

**New open decision for the maintainer:** does Phase 3 (or a sibling phase) also ship an operator surface for the **epoch** axis — the actual home of test-run isolation now — or does `make reset` remain the run-isolation tool until a later phase / successor RFC? This plan does not yet cover epoch. Flagged for sequencing alongside the sender-axis-drop and facts-by-subject follow-ups tracked in [Memory Scope Axes §Consequences](../memory-scope-axes.md#consequences-for-the-current-code-and-rfc-0031).

> Inline `(agent, channel, user)` references in the sections below predate this amendment; the unit is now `(agent, channel)`.

---

## The session model the CLI sits on top of

Before the per-PR detail, the reconciliation the whole phase depends on. After Phases 1–2 + ISSUE-0081/0082, **three** session-bearing mechanisms exist; Phase 3 adds the operator-facing fourth (the pointer file + verbs) and one override:

| Mechanism | Set by | Governs | Lands in |
|-----------|--------|---------|----------|
| `PERSATRIX_SESSION_ID` env var | operator, at boot | the orchestrator + persona-runtime **construction-time seed / single-session fallback** | Phase 1 (v0.3.1) |
| Per-request `(agent, channel, user)` binding | orchestrator, auto-minted on first sight | the **live channel-dispatch path** — emitted as the `persatrix-session` gRPC header, overrides the construction snapshot per request | ISSUE-0082 Part 1 (v0.3.5) |
| `~/.persatrix/active-session` pointer file (+ `PERSATRIX_ACTIVE_SESSION_FILE`) | operator, via `persatrix session use` / `new --activate` | the boot seed when no env var is set, and the CLI default for `--session` | **Phase 3 (this plan, PR 3)** |
| `--session` flag on `chat` / `channel send` / `channel reply` | operator, per invocation | a one-invocation **override**, above the auto-binding | **Phase 3 (this plan, PR 4)** |

The [RFC OQ #6](0031-per-session-namespacing-channels.md#open-questions) precedence chain — `--session` flag > `PERSATRIX_SESSION_ID` env var > `~/.persatrix/active-session` file > built-in `legacy` — is the **resolution order for a process-lifetime / single-conversation session**. The ISSUE-0082 per-request binding is a distinct, per-conversation axis that sits on top of it on the dispatch path; PR 4 reconciles the two by making the explicit `--session` flag the highest-precedence signal the orchestrator honours, above the auto-binding, for the one invocation it accompanies. Absent `--session`, the auto-binding stands (concurrent conversations stay isolated — the Phase 2 + ISSUE-0082 guarantee is not regressed). This is recorded as the [OQ #6 amendment](#open-question-status).

The verbs split cleanly along the REST-vs-local line this implies:

- **Registry verbs** (`new`, `list`, `archive`) operate on the `sessions` table in `channels.db` → orchestrator REST (PR 1 surface, PR 2 client).
- **Pointer verbs** (`use`, `current`, and `new --activate`'s side effect) operate on the local `~/.persatrix/active-session` file → CLI-local (PR 3), with `use` resolving its `<id-or-label>` argument against the registry via REST first.

---

## Dependency Graph

```
PR 1 (orchestrator /api/v1/sessions REST surface over the channels.db registry; reserved-legacy guard)
  ↓
PR 2 (Rust CLI registry verbs: session new / list / archive → PR 1 endpoints)
  ↓
PR 3 (active-session pointer file + PERSATRIX_ACTIVE_SESSION_FILE; session use / current; new --activate side effect)
  ↓
PR 4 (--session override on chat / channel send / channel reply; orchestrator honours it above the auto-binding)
  ↓
PR 5 (closeout: end-to-end CLI integration test; --all-sessions carve-out issue; RFC/ROADMAP/master-plan status)
```

PR 1 must precede PR 2 — the CLI cannot call an endpoint that does not exist. PR 2 → PR 3: `session use`/`current` render the label the registry holds, so the registry client lands first. PR 3 → PR 4: `--session`'s default value is the pointer file, so the file resolver lands first. PR 5 depends on all four — the integration test drives the full verb set against a live orchestrator.

---

## Prerequisites

Satisfied as of the v0.3.5 Phase 2 closeout ([#461](https://github.com/mkhomutov/Persatrix/pull/461)):

1. **RFC 0031 Phase 2 merged** — recall filtering is live; the `sessions="*"` sentinel is library/test-only, with no operator entry point (this plan keeps it that way; see the carve-out above).
2. **The `sessions` registry exists and is populated** — `channels.db` migration v3 (Phase 1) created the table; the ISSUE-0082 PR 1 `SessionResolver` ([`internal/channels/session_binding.go`](../../internal/channels/session_binding.go)) registers every auto-minted id in it, so `session list` will surface auto-created sessions on day one, not just operator-created ones.
3. **The boot/REST session-stamp wiring exists** — `Server.channelSessionID` is already threaded through `handleCreateChannel` / `handlePublishMessage` / `handleChat` ([`internal/server/channel_session_handler_test.go`](../../internal/server/channel_session_handler_test.go)), so PR 4's `--session` override extends a path that is already session-aware rather than introducing one.

---

## Open-question status

[RFC §Decision/Next Steps](0031-per-session-namespacing-channels.md#decision--next-steps) flags the open questions that gate the operator surface. Resolutions consumed by this phase:

| OQ | Topic | Resolution | Consumed by |
|----|-------|------------|-------------|
| **#2** | The `legacy` sentinel as a collidable operator label | **2a** — reject `legacy` (and the reserved-id list) as a `--label`/id at `session new` time. Keeps the §D `session_id = 'legacy'` carve-out from silently merging an operator session into the always-visible pre-RFC namespace. Sentinel value stays human-readable in raw row dumps (vs. the `__legacy__` alternative). | PR 1 (server-side validation, the authoritative guard) + PR 2 (client-side fail-fast message). |
| **#5** | Should `persatrix session delete` exist? | **No.** Archive is one-way ([RFC §B](0031-per-session-namespacing-channels.md#b-session-lifecycle)); row deletion is compliance erasure (RFC 0013) territory, not the operator-convenience surface. No `delete` verb; `archive` marks inactive without deleting rows. | Scope exclusion (PR 1 / PR 2 — `archive` only). |
| **#6** | Active-session resolution precedence | `--session` flag > `PERSATRIX_SESSION_ID` env var > `~/.persatrix/active-session` file > built-in `legacy`. **Amendment (this plan):** the precedence chain governs the *process-lifetime / single-conversation* session; the ISSUE-0082 per-request `(agent, channel, user)` auto-binding is a distinct per-conversation axis that stands on the dispatch path *unless* an explicit `--session` flag accompanies the invocation, in which case the flag wins for that one invocation. The `--all-sessions` debug entry point referenced in the original OQ #6 prose is **carved out** (see [the carve-out note](#persatrix-memory-recall---all-sessions-is-carved-out-not-built-here)). | PR 4 (CLI precedence resolution + orchestrator override). |

OQ #1 / #4 / #7 were consumed by Phase 2; OQ #3 / #8 resolved upstream (Phase 1). No OQ gates this plan beyond the three above.

---

## PR Sequence

### PR 1: `feature/v035-rfc0031p3-rest` — Orchestrator `/api/v1/sessions` REST Surface

**Depends on**: Nothing (RFC 0031 Phase 1 `sessions` table + ISSUE-0082 PR 1 registry-registration merged).
**Purpose**: Expose the `sessions` registry in `channels.db` over REST so the thin Rust CLI can create, list, and archive sessions without touching SQLite. Pure orchestrator-side addition — no CLI caller yet, so no operator-visible behaviour change (mirrors how ISSUE-0082 PR 1 shipped the resolver ahead of the dispatch wiring).

#### Scope

| File | Change |
|------|--------|
| [`internal/channels/sqlite_schema.go`](../../internal/channels/sqlite_schema.go) / a sibling query file | `ChannelStore` (or a focused `SessionRegistry` accessor over the same DB) gains `CreateSession(ctx, label string) (Session, error)`, `ListSessions(ctx, includeArchived bool) ([]Session, error)`, `GetSession(ctx, idOrLabel string) (Session, error)`, `ArchiveSession(ctx, idOrLabel string) error`. `CreateSession` mints a UUIDv7 id ([matching the §B mint and the ISSUE-0082 resolver](0031-per-session-namespacing-channels.md#b-session-lifecycle), so ids sort lexicographically by creation time — the default `list` order); reuse the existing `sessions`-table writer the resolver already uses (no second mint path). |
| `internal/server/session_handlers.go` (new) | `handleListSessions` (`GET /api/v1/sessions?include_archived=`), `handleCreateSession` (`POST /api/v1/sessions`), `handleGetSession` (`GET /api/v1/sessions/{id}` — resolves id-or-label, for `use`/`current` label rendering), `handleArchiveSession` (`POST /api/v1/sessions/{id}/archive`). Mirror the [`channel_handlers.go`](../../internal/server/channel_handlers.go) handler shape (JSON in/out, `channel_errors.go`-style error envelopes). |
| [`internal/server/server.go`](../../internal/server/server.go) | Register the four routes alongside the `/api/v1/channels` routes. |
| [`internal/server/types.go`](../../internal/server/types.go) | `CreateSessionRequest{ label string }`, `SessionResponse{ id, label, created_at, archived bool }`, `ListSessionsResponse{ sessions []SessionResponse }`. |
| `internal/server/session_handlers_test.go` (new) | Create → list round-trip; `include_archived` filters; `archive` flips the flag without deleting rows; `GET {id}` resolves by both id and label; **reserved-`legacy` label/id rejected with 4xx (OQ #2a)**; auto-minted ids (seeded directly via the resolver) appear in `list`. |

#### Key implementation details

- **Reserved-id guard (OQ #2a) is server-authoritative.** Reject `legacy` (and any future reserved sentinel) as a `label` *or* explicit id at create time, returning a 4xx the CLI surfaces verbatim. The client also fails fast (PR 2), but the server is the guard of record — a direct REST caller must not be able to mint a `legacy`-labelled row that collides with the §D carve-out.
- **No new mint path.** `CreateSession` delegates to the same `sessions`-table insert the ISSUE-0082 [`SessionResolver`](../../internal/channels/session_binding.go) already performs, so operator-created and auto-minted sessions are indistinguishable rows in one registry. The binding table (`(agent, channel, user) → session_id`) is untouched — operators create *registry* rows, not bindings.
- **Society state (RFC 0029 §G.1).** The registry lives with `channels` in `channels.db`; when RFC 0029 Phase 3 moves the society store to Postgres, these endpoints move with it — no redesign.
- **No archive back-edge.** Archive is one-way ([RFC §B](0031-per-session-namespacing-channels.md#b-session-lifecycle)); there is no `unarchive`/`activate` REST verb (OQ #5).

#### Tests

- Create returns a UUIDv7 id; two creates sort lexicographically by `created_at`.
- `GET /api/v1/sessions` lists active only; `?include_archived=true` includes archived.
- `POST {id}/archive` flips `archived`; the row's `session_id` is still resolvable and its tagged memory rows are untouched.
- `POST /api/v1/sessions` with `label=legacy` → 4xx (OQ #2a); same for the explicit-id form.
- A session seeded via the auto-mint resolver appears in `list`.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] Reserved-`legacy` label/id rejected server-side with a 4xx.
- [ ] `grep` confirms no CLI caller yet — the endpoints are an enabler.
- [ ] [RFC 0031 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening (resuming from the Phase 2 `⚠️ Partially Implemented` pause); [RFC 0031 file](0031-per-session-namespacing-channels.md) `status:` frontmatter **and** the `**Status**:` heading flip in the same PR, per [Status Hygiene Rule 1](../development-workflow.md#status-hygiene); regenerate [INDEX.md](INDEX.md) via `make rfcs`.

---

### PR 2: `feature/v035-rfc0031p3-cli-registry` — CLI Registry Verbs (`session new` / `list` / `archive`)

**Depends on**: PR 1 merged.
**Purpose**: Add the `persatrix session` subcommand group with the three registry verbs that talk to PR 1's REST surface. No active-session file yet — `new` mints + registers but does not activate; `--activate` lands in PR 3.

#### Scope

| File | Change |
|------|--------|
| [`cli/src/main.rs`](../../cli/src/main.rs#L27) | Add `Session(SessionCommands)` to the top-level `Commands` enum (between `Cost` and `State`); add the dispatch arm to the exhaustive `match` (~L264). |
| `cli/src/commands/session.rs` (new) | `SessionCommands` clap subcommand enum (`New { label: Option<String> }`, `List { include_archived: bool, json: bool }`, `Archive { id_or_label: String }`) + a `dispatch()` fn, following the [`channel_dispatch.rs`](../../cli/src/commands/channel_dispatch.rs#L119) grouped-subcommand pattern. Each verb is an `async fn cmd_session_*(client, server, …) -> Result<(), String>` over `reqwest`, mirroring [`workflow.rs`](../../cli/src/commands/workflow.rs#L11). |
| [`cli/src/commands/mod.rs`](../../cli/src/commands/mod.rs) | `pub(crate) mod session;`. |
| [`cli/src/types.rs`](../../cli/src/types.rs) | `CreateSessionRequest` / `SessionResponse` / `ListSessionsResponse` serde structs matching PR 1's wire shape (`#[serde(default)]` + `skip_serializing_if` for forward-compat, per the existing convention); `tabled`-rendered `list` output. |
| [`cli/src/validation.rs`](../../cli/src/validation.rs#L20) | `session new --label X` runs `validate_resource_id(X, "session label")` and **fails fast on `legacy`** (client-side mirror of PR 1's server guard — friendlier message, but the server stays authoritative). |
| `cli/src/commands/session.rs` `#[cfg(test)]` | Serde round-trip contract tests for the three request/response types against PR 1's JSON shape; `tabled` rendering snapshot for `list` (the in-file unit-test pattern [`types.rs`](../../cli/src/types.rs#L140) uses — no `assert_cmd`/`insta` in this repo). |

#### Key implementation details

- **REST-only — no SQLite, no homedir.** These three verbs are pure REST calls; nothing touches `~/.persatrix/` (that is PR 3). Keeps PR 2 reviewable as "the registry client" with no filesystem surface.
- **Label uniqueness lands here, with a known resolution edge to close.** PR 1 deliberately left labels nullable/non-unique (the schema has no `UNIQUE`, and the auto-mint path writes NULL labels); `GetSession` pins "duplicate labels resolve to the lowest id" as a *characterization* test, not a desired contract. The robust fix — a migration-backed partial unique index on non-NULL labels — belongs here with the `session new` UX (a check-then-insert at PR 1 would have been a racy half-measure). Until that index exists, note the edge it closes: with duplicate labels, `GetSession` resolves lowest-id **ignoring `archived` state**, so `use <label>`/`archive <label>` can land on an archived row while an active namesake exists (`archive` would then no-op on the already-archived row, leaving the active one un-archived). The unique index eliminates the duplicate, and with it this edge; until then it is bounded to the unmanaged-duplicate case PR 2 forbids.
- **`list` default order** matches the UUIDv7 lexicographic-by-creation order PR 1 returns; `--json` emits the raw response for scripting, matching the `channel list --json` precedent.
- **Error surfacing** uses the existing `api_error_message(resp)` ([`types.rs`](../../cli/src/types.rs#L130)) so PR 1's reserved-`legacy` 4xx renders as a clean operator error, not a panic.

#### Tests

- `CreateSessionRequest` / `SessionResponse` / `ListSessionsResponse` serde round-trips match the Go wire shape.
- `session new --label legacy` fails fast client-side with the reserved-label message.
- `list` rendering snapshot (active-only and `--include-archived`).

#### PR checklist

- [ ] `make test` passes (`cargo test` + Go); `make lint` clean.
- [ ] `persatrix session new` / `list` / `archive` work against a live orchestrator.
- [ ] No `~/.persatrix/` access in this PR (`grep` confirms the file surface is deferred to PR 3).

---

### PR 3: `feature/v035-rfc0031p3-active-file` — Active-Session Pointer File + `use` / `current` / `--activate`

**Depends on**: PR 2 merged.
**Purpose**: Land the `~/.persatrix/active-session` pointer file (with the `PERSATRIX_ACTIVE_SESSION_FILE` override) and the two pointer verbs, plus `new --activate`'s side effect. This is the second of the three resolution mechanisms — the file — coming online.

#### Scope

| File | Change |
|------|--------|
| `cli/src/active_session.rs` (new) | The pointer-file helper: `path()` resolves `PERSATRIX_ACTIVE_SESSION_FILE` else `~/.persatrix/active-session`; `read() -> Option<String>`; `write(id)` (creates `~/.persatrix/` if absent); `clear()`. Home-dir resolution needs a new dep — add `dirs = "5"` to [`cli/Cargo.toml`](../../cli/Cargo.toml) (no homedir crate today; declare the minimal addition with a one-line rationale comment, matching the existing dependency-hygiene notes there). |
| `cli/src/commands/session.rs` | Add `Use { id_or_label: String }` and `Current` to `SessionCommands`. `use` resolves `<id-or-label>` via PR 1's `GET /api/v1/sessions/{id}` (reject unknown / archived with a clear message — [RFC §Security misconfig risk](0031-per-session-namespacing-channels.md#security-considerations)), then `active_session::write(resolved_id)` and logs the active id at INFO. `current` reads the file, enriches with the registry label via REST, and prints (or "no active session — using `legacy`"). Add `--activate` to `New`; on success it calls `active_session::write(new_id)`. |
| `cli/src/active_session.rs` `#[cfg(test)]` | Pointer round-trip (`write` → `read`); `PERSATRIX_ACTIVE_SESSION_FILE` override beats the default path; `read()` on a missing file returns `None`; `clear()` removes the pointer. Use a `TempDir` + the env override so tests never touch the real `~/.persatrix/`. |

#### Key implementation details

- **`use` resolves against the registry, then writes locally.** The argument is an id *or* a label (RFC §E `use <id-or-label>`); resolution is the PR 1 `GET {id}` round-trip, so a typo or archived target fails before the pointer is written — not after, when it would silently misroute new channels ([RFC §Security misconfig risk](0031-per-session-namespacing-channels.md#security-considerations)).
- **The file is CLI-local; the orchestrator consumes it at boot.** Writing the pointer changes which session the *next* orchestrator boot seeds from (and which value PR 4 defaults `--session` to); it does **not** live-rebind in-flight processes ([RFC §B](0031-per-session-namespacing-channels.md#b-session-lifecycle) — "in-flight processes continue under the session they started with"). For co-located dev (CLI + orchestrator share a filesystem) the boot read already exists via `PERSATRIX_SESSION_ID`; this plan does **not** add an orchestrator file-reader — the pointer's load-bearing consumer in v0.3.5 is the CLI's own `--session` default (PR 4). *(Recorded as a decision: keep the orchestrator's boot session sourced from the env var; the file is the operator's source of truth that the CLI reads and forwards, avoiding a CLI/orchestrator shared-filesystem assumption in non-co-located deployments. Flagged for the maintainer.)*
- **INFO logging on activation** per [RFC §Security misconfig risk](0031-per-session-namespacing-channels.md#security-considerations) — a stale pointer at an archived session is the documented footgun; logging the active id at `use`/`--activate` time is the mitigation.

#### Tests

- `write` → `read` round-trip; `PERSATRIX_ACTIVE_SESSION_FILE` override resolves ahead of the default.
- `use <label>` resolves the label to its id before writing; unknown / archived target errors without writing.
- `current` with no pointer prints the `legacy` fallback; with a pointer prints the registry label.
- `new --activate` writes the pointer; `new` without it does not.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] Tests use a `TempDir` + `PERSATRIX_ACTIVE_SESSION_FILE` — no test touches the real `~/.persatrix/`.
- [ ] `dirs` is the only new dependency; rationale comment in `Cargo.toml`; `make notices` regenerated if the dependency graph changed (run with the venv interpreter per project memory).

---

### PR 4: `feature/v035-rfc0031p3-session-override` — `--session` Override on `chat` / `channel`

**Depends on**: PR 3 merged.
**Sequencing caveat**: ✅ **settled** — the [sender-axis drop (ISSUE-0083)](../issues/ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md) **shipped 2026-05-30, before this PR**, so the binding key is already `(agent, channel)`. PR 4's inline `(agent, channel, user)` references read `(agent, channel)`; the override sits above the auto-binding regardless of its key.
**Purpose**: Add `--session` to `persatrix chat` / `persatrix channel send` / `persatrix channel reply`, resolve the [OQ #6](0031-per-session-namespacing-channels.md#open-questions) precedence chain CLI-side, forward the resolved id to the orchestrator, and make the orchestrator honour it **above the per-request auto-binding** for that invocation. This is the reconciliation PR — it makes the explicit operator signal the highest-precedence one without regressing the Phase 2 + ISSUE-0082 concurrent-isolation guarantee.

#### Scope

| File | Change |
|------|--------|
| `cli/src/session_resolve.rs` (new) | `resolve_session(flag: Option<&str>) -> Option<String>` implementing OQ #6: `--session` flag > `PERSATRIX_SESSION_ID` env > `active_session::read()` file > `None` (orchestrator applies the `legacy` default). One helper, shared by both call sites, so the precedence cannot drift. |
| [`cli/src/main.rs`](../../cli/src/main.rs) | `--session <id-or-label>` arg on the `Chat` and `Channel` (publish/list) variants; thread the resolved value into the request. |
| [`cli/src/commands/chat.rs`](../../cli/src/commands/chat.rs), [`cli/src/commands/channel.rs`](../../cli/src/commands/channel.rs) | Send the resolved session to the orchestrator (new optional `session_id` request field on the publish/chat REST bodies, or the existing `persatrix-session` wire key — see the key detail below). |
| [`internal/server/channel_handlers.go`](../../internal/server/channel_handlers.go), [`internal/server/chat_handler.go`](../../internal/server/chat_handler.go) | When the request carries an explicit session override, use it **instead of** `Server.channelSessionID` (the boot default) and pass it down the dispatch path so it is the value emitted as the `persatrix-session` header — overriding the [ISSUE-0082 auto-binding](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) for that one request. Absent the override, behaviour is byte-identical to today (auto-binding stands). |
| [`internal/channels/grpc_dispatcher.go`](../../internal/channels/grpc_dispatcher.go) | The `Dispatch` chokepoint accepts a request-scoped session override (threaded from the handler via context) and prefers it over `SessionResolver.Resolve(...)` when present. |
| `internal/server/channel_session_handler_test.go` (extend) + `internal/channels/grpc_dispatcher_test.go` (extend) | An explicit override emits `persatrix-session == override`, not the auto-binding; absent the override the auto-binding is still emitted (no regression); two concurrent dispatches — one with override, one without — emit independent ids. |
| `cli/src/session_resolve.rs` `#[cfg(test)]` | Precedence: flag beats env beats file beats `None`; each layer exercised. |

#### Key implementation details

- **The reconciliation: explicit beats automatic, for one invocation.** The ISSUE-0082 auto-binding exists to isolate concurrent conversations that share one process. An operator who passes `--session run-arc-3` is deliberately asking for a *specific* session (e.g., re-binding a dementia-test arc across runs — [RFC OQ #1 resolution 1a](0031-per-session-namespacing-channels.md#open-questions)); that explicit intent must win. PR 4 makes the override the top of the chain on the dispatch path **only when present**, so the default concurrent-isolation property is untouched — the override is additive on the explicit path, exactly mirroring how ISSUE-0082 PR 2 made emission additive on the happy path.
- **Wire transport decision.** The override travels CLI→orchestrator on the REST request, then orchestrator→persona on the existing `persatrix-session` gRPC header (the cross-language key at [`agents/session_id.py::SESSION_METADATA_GRPC_KEY`](../../agents/session_id.py)) — reusing the ISSUE-0082 rail rather than inventing a second one. *(The REST-side carrier — a `session_id` body field vs. a request header — is a small decision flagged for the PR thread; the body field matches the existing `CreateChannelRequest`/`PublishMessageRequest` shape and keeps the wire self-describing.)*
- **`--session` accepts id-or-label** for parity with `session use`; label resolution reuses PR 1's `GET {id}`. A label that resolves to an archived session warns but proceeds (the operator explicitly named it) — distinct from `use`, which refuses to *activate* an archived session.
- **No principal axis.** `--session` is the session override only; `persatrix-principal` stays unemitted until [RFC 0039](0039-user-accounts-authentication.md) (ISSUE-0082 §Future Work).

#### Tests

- CLI precedence: flag > env > file > none, each layer pinned.
- Override emitted as `persatrix-session`; absent override → auto-binding still emitted; concurrent override + non-override dispatches stay independent.
- `channel send --session <label>` resolves the label; archived-label warns-but-proceeds.

#### PR checklist

- [ ] `make test` passes; `make lint` clean.
- [ ] Override precedence matches OQ #6; the auto-binding is unchanged when no override is present (no concurrent-isolation regression).
- [ ] Emitted override string-matches `agents.session_id.SESSION_METADATA_GRPC_KEY` (cross-language contract asserted as a literal, per the ISSUE-0082 PR 2 discipline).
- [ ] `channel.dispatch` span carries the resolved session id (low-cardinality-on-span, per OQ #7).

---

### PR 5: `feature/v035-rfc0031p3-close` — Closeout + `--all-sessions` Carve-Out

**Depends on**: PR 4 merged.
**Sequencing caveat**: ✅ the [sender-axis drop (ISSUE-0083)](../issues/ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md) has landed (2026-05-30), so a multi-party recall leg — if added — must assert the post-reframing "two senders in one room → one shared room session"; the pre-reframing "two senders → distinct" behaviour no longer exists to pin against (see [Amendment](#amendment--scope-axes-reframing)).
**Purpose**: Prove the verb set end-to-end through the live REST path, carve out the deferred `--all-sessions` debug verb as a tracked issue, and land the documentation/status closeout. No production code.

#### Scope

| File | Change |
|------|--------|
| `tests/integration/test_session_operator_surface.py` (new) | The Phase 3 acceptance gate: drive `session new --label arc → use → current → list → archive` against a **live** orchestrator (real `bin/persatrix-server` + `persatrix` binaries, ephemeral ports, per-test `--channels-db` + `PERSATRIX_ACTIVE_SESSION_FILE`) and assert the registry + pointer-file state at each step, plus the OQ #2a reserved-`legacy` guard end-to-end and an archived session staying resolvable (`GET {id}` 200; `current` renders the archived marker — RFC 0031 §B). Opt-in via `-m requires_orchestrator` (the `test_logs_e2e.py` harness shape); collected by the `tests/integration/` CI step ([ISSUE-0076](../issues/ISSUE-0076-full-integration-suite-not-run-in-ci.md)) and skipped there when the binaries are not built. **Scope note:** this orchestrator-only harness does not stand up the persona society, so the `--session` *override-beats-auto-binding* + *recall-isolation* legs are pinned where they live — `internal/server/channel_session_handler_test.go` + `internal/channels/grpc_dispatcher_test.go` (PR 4) and `tests/integration/test_session_emission_isolation.py` (ISSUE-0082 PR 3) — rather than re-stood-up here (the same reasoning `test_session_emission_isolation.py` records for declining to start the Go binary). |
| [`docs/issues/ISSUE-0086-operator-all-sessions-recall-verb.md`](../issues/ISSUE-0086-operator-all-sessions-recall-verb.md) (new) | Carve out `persatrix memory recall --all-sessions` (the only operator route to `sessions="*"`) as a follow-up: it needs an operator memory-inspection surface (CLI verb + orchestrator REST + persona-side recall RPC) that does not exist; until it ships, the `"*"` sentinel keeps no operator entry point — provably unreachable from a prompt context. Mirrors the [Phase 4 `legacy-prune` carve-out](0031-per-session-namespacing-channels.md#phase-4-cleanup-and-documentation-pass). |
| [`docs/rfcs/0031-per-session-namespacing-channels.md`](0031-per-session-namespacing-channels.md) | §E status update: the file + flag resolution mechanisms are now wired; record the OQ #6 amendment (override-above-auto-binding) and that the `--all-sessions` entry point is deferred. Body `**Status**:` heading advances Phase 3 → shipped; Phase 4 docs + ISSUE-0051 closeout remain (RFC overall still `🚧 Implementing`). |
| [`docs/v0.3.5-plan.md`](../v0.3.5-plan.md) | [Master Progress Overview](../v0.3.5-plan.md#master-progress-overview) row 2 → ✅ Merged with the final PR date. |
| [`ROADMAP.md`](../../ROADMAP.md) | Status-hygiene refresh for the Phase 3 work; RFC 0031 stays `🚧 Implementing` (Phase 4 remains). |

No production code in PR 5 — test + docs only.

#### Key implementation details

- **The integration test is the regression pin** for the operator surface: the verb lifecycle, the pointer-file read/write, the reserved-`legacy` guard, and an archived session's row surviving (resolvable + marked) across archive. The override-beats-auto-binding reconciliation and the cross-session recall isolation are pinned upstream (PR 4 Go tests + the ISSUE-0082 emission-isolation Python gate), since this orchestrator-only harness does not run the persona society.
- **The carve-out is deliberate scope discipline**, not an omission: building the `"*"` operator route is a separate "operator memory inspection" story, and leaving it unbuilt is the stronger security posture (no operator entry point to the all-sessions sentinel).
- **`make reset` deprecation breadcrumb is Phase 4's, not this PR's.** [RFC §E Phase 3 deliverable 4](0031-per-session-namespacing-channels.md#phase-3-operator-cli) lists the breadcrumb, but the [v0.3.5 master plan §Phase 3](../v0.3.5-plan.md#phase-3--rfc-0031-phase-4-operator-docs--issue-0051-closeout) assigns the `channels.md` / `persona-agents.md` breadcrumb + `docs/guides/sessions.md` to the Phase 4 docs pass, which lands only after all three resolution mechanisms are wired. This plan defers it there to avoid a doc that describes the file mechanism before PR 3 ships it.

#### PR checklist

- [x] `make test` passes; the new integration test runs in the full `tests/integration/` suite (opt-in `-m requires_orchestrator`; skips cleanly when binaries are absent, mirroring `test_logs_e2e.py`).
- [x] `--all-sessions` carve-out issue filed ([ISSUE-0086](../issues/ISSUE-0086-operator-all-sessions-recall-verb.md)) and linked from RFC §E + the issues index.
- [x] RFC §E reflects file + flag wired; OQ #6 amendment recorded; RFC Phase 3 marked shipped, Phase 4 (docs) + ISSUE-0051 closeout remain.
- [x] [v0.3.5-plan.md](../v0.3.5-plan.md) Master Progress Overview row 2 → ✅ Merged.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The `--session` override silently defeats the ISSUE-0082 concurrent-isolation fix (re-introducing cross-conversation bleed against the very property Phase 2 shipped). | The override is applied **only when present**; absent it, the auto-binding is byte-identical to today. PR 4 pins both halves: override-emitted-when-present **and** auto-binding-emitted-when-absent, plus a concurrent override/non-override test. |
| The operator creates a `legacy`-labelled session, silently merging their session into the always-visible §D carve-out (recall-correctness footgun — RFC OQ #2). | Server-authoritative reserved-id rejection (PR 1) + client-side fail-fast (PR 2). The server is the guard of record so a direct REST caller cannot bypass it. |
| A stale `~/.persatrix/active-session` pointing at an archived session misroutes new channels (RFC §Security misconfig risk). | `use` resolves + validates the target against the registry before writing (refuses archived); `use`/`--activate` log the active id at INFO; the Phase 4 guide documents the file location. |
| The CLI assumes a shared filesystem with the orchestrator (the `~/.persatrix/active-session` boot-read in §E), breaking non-co-located deployments. | This plan does **not** add an orchestrator file-reader; the pointer's load-bearing consumer is the CLI's own `--session` default, forwarded over REST. The orchestrator boot session stays env-var-sourced. (Decision flagged for the maintainer in PR 3.) |
| Scope creep: the `--all-sessions` debug verb pulls an entire operator memory-inspection surface into the session story. | Carved out as a follow-up issue (PR 5); the `"*"` sentinel keeps no operator entry point — stronger than the Phase 2 guarantee, not weaker. |

---

## Progress Overview

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| — | This plan (Phase 3 PR plan authoring) | `feature/v035-rfc0031p3-plan` | ✅ Merged | [#462](https://github.com/mkhomutov/Persatrix/pull/462) | 2026-05-30 |
| 1 | Orchestrator `/api/v1/sessions` REST surface | `feature/v035-rfc0031p3-rest` | ✅ Merged | [#464](https://github.com/mkhomutov/Persatrix/pull/464) | 2026-05-30 |
| 2 | CLI registry verbs (`new` / `list` / `archive`) | `feature/v035-rfc0031p3-cli-registry` | ✅ Merged | [#466](https://github.com/mkhomutov/Persatrix/pull/466) | 2026-05-30 |
| 3 | Active-session pointer file + `use` / `current` / `--activate` | `feature/v035-rfc0031p3-active-file` | ✅ Merged | [#467](https://github.com/mkhomutov/Persatrix/pull/467) | 2026-05-30 |
| 4 | `--session` override on `chat` / `channel` | `feature/v035-rfc0031p3-session-override` | ✅ Merged | [#469](https://github.com/mkhomutov/Persatrix/pull/469) | 2026-05-30 |
| 5 | Closeout + `--all-sessions` carve-out | `feature/v035-rfc0031p3-close` | 🔀 PR open | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](0031-per-session-namespacing-channels.md) — §E operator surface is this phase's contract.
- [RFC 0031 PR plan (Phase 2)](0031-phase2-pr-plan.md) — the recall-filtering workstream this phase builds the operator surface for.
- [v0.3.5 master plan](../v0.3.5-plan.md) — the umbrella; §Phase 2 names this plan as its deliverable.
- [ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md) / [ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) — the per-request session model this CLI sits on top of.
- [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md) — root issue; closes at Phase 4.
- [BRANCHING.md](../BRANCHING.md) — branching / squash-merge convention.
