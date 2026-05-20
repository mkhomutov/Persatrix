# RFC 0023 — PR Implementation Plan (LLM Call Leasing — full, Phases 1–6)

**RFC**: [0023-llm-call-leasing.md](0023-llm-call-leasing.md)
**Created**: 2026-05-17
**Branch prefix**: `feature/v032-rfc0023-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Master plan**: [v0.3.2-plan.md Phase 1 (combined plans PR)](../v0.3.2-plan.md#phase-1--author-the-two-rfc-pr-plans)

---

## Overview

RFC 0023 moves the orchestrator from a post-hoc cost accountant to an in-line gatekeeper: every LLM invocation acquires a server-issued lease from a Go-side `WalletService` over gRPC before issuing, and settles actual usage afterward. Today the only enforcement point is a single pre-dispatch `CheckBudget` per `ExecuteTask`; chat, autonomous TICK, and sub-agent spawns bypass it entirely ([RFC §A](0023-llm-call-leasing.md#a-current-state-and-gaps)). The lease protocol makes "does this path acquire a lease?" a code-review-able invariant and flips the failure mode from silent overspend to loud rejection.

The RFC ships in full under v0.3.2 — all six phases of the [RFC §Phased Implementation Plan](0023-llm-call-leasing.md#phased-implementation-plan). This plan splits the work into **8 PRs**: six implementation PRs that map one-to-one onto the RFC's six phases, plus a review-follow-ups PR and a full-RFC closeout PR, mirroring the [RFC 0017 PR plan](0017-pr-plan.md) structure. Each PR leaves the repo in a passing-tests, lint-clean state and stays within the [BRANCHING.md](../BRANCHING.md) size guidance.

**Prerequisite**: RFC 0006 (Efficiency & Execution Limits — closed) and RFC 0017 (Memory Injection Budget — closed) merged. RFC 0011 (Channels & Bridges) merged — Phase 6 (channel-message origin) wires the lease into the already-shipped RFC 0011 response-gate path.

**Phase 0 hard gate**: PR 1 does **not** open until [RFC 0023 OQ #1](0023-llm-call-leasing.md#open-questions) (transport) and [OQ #5](0023-llm-call-leasing.md#open-questions) (tokeniser parity) resolve in the RFC review thread. See [§Phase 0 Hard Gate](#phase-0-hard-gate) below — this is the single load-bearing gate the [v0.3.2 master plan](../v0.3.2-plan.md#risk-and-mitigations) tracks for this workstream.

**Recommended merge order**: **PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 7 → PR 8**. PRs 1–2 land the contract and the enforcement engine with no call-site wiring. PRs 3–6 wire the four call origins in the [RFC §Phased Implementation Plan](0023-llm-call-leasing.md#phased-implementation-plan) order — workflow, chat, TICK + sub-agent, channel-message — each reusing the `WalletClient` introduced by PR 3. PRs 4–6 touch disjoint call sites and could in principle land in any order once PR 3 merges; the recommended order tracks the RFC's phase numbering and keeps each diff reviewable.

---

## Phase 0 Hard Gate

[RFC 0023 §Decision/Next Steps](0023-llm-call-leasing.md#decision--next-steps) promoted two open questions to **Phase 0 blockers** — both change the proto contract and the Python client surface, so they are non-additive once PR 1 ships. **Both resolved 2026-05-18** in [RFC 0023 §Open Questions](0023-llm-call-leasing.md#open-questions) §1 and §5; the gate is cleared and PR 1 may open.

- **[OQ #1](0023-llm-call-leasing.md#open-questions) — transport.** **Resolved: outbound dial, reusing the existing `LogService` channel.** `WalletService` registers on the orchestrator-side gRPC listener that already hosts `LogService`; the agent's wallet client reuses the channel it already opens for log streaming (`--orchestrator-grpc`), so no new connection is added. PR 1 registers `WalletService` on that listener; PR 3's `agents/server.py` startup path retains the shared channel and adds the wallet stub.
- **[OQ #5](0023-llm-call-leasing.md#open-questions) — tokeniser parity.** **Resolved: promote `tiktoken` to a hard runtime dependency.** It moves into the runtime `dependencies` list in `agents/pyproject.toml`; the now-empty `accurate-tokens` extra and the duplicate `tiktoken` entry in the `dev` extra are removed in the same change. `LLMClient` reuses the existing `cl100k_base` `_count_tokens` helper for `estimated_input_tokens`, and `LeaseRequest.estimated_input_tokens` carries that tiktoken count.

Both were calibration choices with provisional answers in the RFC; the resolutions are recorded in [RFC 0023 §Open Questions](0023-llm-call-leasing.md#open-questions) §1 and §5, which PR 1's [Progress Overview](#progress-overview) row references.

---

## Dependency Graph

```
[Phase 0 hard gate: OQ #1 (transport) + OQ #5 (tokeniser parity) resolved]
  ↓
PR 1 (proto/wallet.proto + generated stubs + internal/wallet/ skeleton — always-grant)   [RFC Phase 1]
  ↓
PR 2 (TokenCounter RecordProvisional/Reconcile + BudgetEnforcer wired + reaper)           [RFC Phase 2]
  ↓
PR 3 (agents/wallet_client.py + LLMClient.create_message lease wrap — WORKFLOW_TASK)      [RFC Phase 3]
  ↓
PR 4 (chat path — CHAT cause; closes the v0.2.3 bypass; MT-COST-003)                      [RFC Phase 4]
  ↓
PR 5 (autonomous TICK + sub-agent — AUTONOMOUS_TICK / SUB_AGENT causes; MT-COST-004)      [RFC Phase 5]
  ↓
PR 6 (channel-message origin — CHANNEL_MESSAGE cause; RFC 0011 response-gate path)        [RFC Phase 6]
  ↓
PR 7 (review follow-ups)
  ↓
PR 8 (full-RFC closeout — status: ✅ Implemented)
```

PRs 1–2 add no call-site wiring — the contract and the enforcement engine are reviewable in isolation. PR 3 introduces `WalletClient` and the `lease()` async context manager; PRs 4–6 reuse it for the remaining three origins. PRs 4, 5, and 6 each hard-depend only on PR 3 — the arrows between them are the recommended review order, not code dependencies; they touch disjoint call sites and could land in any order once PR 3 merges.

---

## PR Sequence

### PR 1: `feature/v032-rfc0023-proto-skeleton` — Proto Surface + Wallet Skeleton

**Depends on**: [Phase 0 hard gate](#phase-0-hard-gate) — [OQ #1](0023-llm-call-leasing.md#open-questions) and [OQ #5](0023-llm-call-leasing.md#open-questions) **resolved 2026-05-18** (gate cleared). No code dependency on prior PRs (builds on the v0.3.1 baseline).
**Purpose**: Land the `WalletService` proto contract and a no-op servicer skeleton. No call-site wiring — establishes the cross-language contract so it is reviewable in isolation. Implements [RFC §Phased Implementation Plan Phase 1](0023-llm-call-leasing.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `proto/wallet.proto` | **New** — `WalletService` with `AcquireLease` / `SettleLease` / `ReleaseLease` and the `LeaseRequest` / `LeaseResponse` / `LeaseGrant` / `LeaseDenied` / `SettlementRequest` / `ReleaseRequest` / `SettlementAck` messages and the `Cause` enum, verbatim from [RFC §C](0023-llm-call-leasing.md#c-proto-surface). `option go_package = ".../internal/generated/walletpb"`. |
| *(generated)* Go + Python wallet stubs | Regenerated via `make proto`; the new `walletpb` Go package lands under `internal/generated/`, Python stubs alongside the existing generated modules. The proto source-of-truth gate (Python freshness + orphan detection) must pass. |
| `internal/wallet/wallet.go` | **New** — `WalletService` gRPC servicer skeleton. `AcquireLease` always returns a `LeaseGrant` with a server-issued ULID `lease_id`; `SettleLease` / `ReleaseLease` always return `SettlementAck{success: true}`. No `BudgetEnforcer` wiring, no provisional charge, no reaper — those are PR 2. |
| `internal/wallet/wallet_test.go` | **New** — skeleton tests pinning the always-grant contract and ULID `lease_id` issuance (Go: failing `_test.go` before `wallet.go`, per the [TDD rule](../../.github/copilot-instructions.md)). |
| `cmd/orchestrator/main.go` | Register `WalletService` on the orchestrator-side gRPC listener that already hosts `LogService` ([OQ #1](0023-llm-call-leasing.md#open-questions) resolved — outbound dial). No listener change beyond registration. |
| `docs/diagrams/component-architecture.md` | Add the `internal/wallet/` package box. |

#### Key implementation details

- The proto is the load-bearing artifact — it is generated for both Go and Python so PRs 2–6 build against a frozen contract. The `Cause` enum ships all five values (`WORKFLOW_TASK`, `CHAT`, `AUTONOMOUS_TICK`, `SUB_AGENT`, `CHANNEL_MESSAGE`) now even though only `WORKFLOW_TASK` is wired before PR 3; adding enum values later is additive but reserving them now keeps the contract stable.
- The skeleton servicer is deliberately inert: it proves registration, stub generation, and the proto gate without taking on enforcement semantics. This isolates "the contract compiles and registers" from "the contract enforces a budget" (PR 2).
- `lease_id` is a server-issued ULID from PR 1 onward so the skeleton's `Settle`/`Release` already exercise the real ID shape.

#### Tests

- `AcquireLease` returns a `LeaseGrant` whose `lease_id` parses as a ULID and whose granted token counts echo the request estimates.
- `SettleLease` / `ReleaseLease` on any `lease_id` return `success: true` (skeleton contract).
- `make proto` produces no diff on a clean re-run (proto freshness gate).

#### PR checklist

- [ ] `make proto` regenerated; proto source-of-truth gate passes (Python freshness + orphan detection).
- [ ] `go test ./internal/wallet/...` passes; `golangci-lint` clean.
- [ ] `proto/wallet.proto` matches [RFC §C](0023-llm-call-leasing.md#c-proto-surface) field-for-field.
- [ ] `WalletService` registered in `cmd/orchestrator/main.go`.
- [ ] No call-site wiring — `agents/` and `internal/scheduler/` untouched.
- [ ] Phase 0 hard gate confirmed cleared — [OQ #1](0023-llm-call-leasing.md#open-questions) + [OQ #5](0023-llm-call-leasing.md#open-questions) resolved in [RFC 0023 §Open Questions](0023-llm-call-leasing.md#open-questions).
- [ ] [RFC 0023 row in ROADMAP](../../ROADMAP.md#rfc-master-index) → `🚧 Implementing` on this PR opening (first implementation PR); [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 2 → 🔄 In progress.

---

### PR 2: `feature/v032-rfc0023-wallet-enforcement` — Real Enforcement + Reaper

**Depends on**: PR 1 merged.
**Purpose**: Make the wallet enforce. Add the provisional-charge / reconcile primitives to `TokenCounter`, compose `BudgetEnforcer` into `AcquireLease`, and add the TTL reaper. Still no agent integration — the wallet is unit-tested in isolation. Implements [RFC §Phased Implementation Plan Phase 2](0023-llm-call-leasing.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `internal/cost/cost.go` | Add `RecordProvisional(rec UsageRecord)` — adds the worst-case estimate to all three scope totals, marked provisional — and `Reconcile(leaseID, actualInput, actualOutput)` — replaces the provisional with the actual, applying the delta atomically ([RFC §D](0023-llm-call-leasing.md#d-go-wallet-service)). Comment `CheckBudget` as composed-by-wallet rather than called directly by `stage_runner`. |
| `internal/cost/cost_provisional_test.go` | **New** — provisional/reconcile semantics: provisional → settle delta correctness, `Release` as `Reconcile(0, 0)`, double-reconcile rejection. |
| `internal/wallet/wallet.go` | Wire `BudgetEnforcer.CheckBudget` into `AcquireLease` under a single coarse `sync.Mutex` held across check-then-`RecordProvisional` ([RFC §D lock-granularity note](0023-llm-call-leasing.md#d-go-wallet-service) — the atomicity the parallel-step optimism must not inherit). Real `SettleLease` / `ReleaseLease` via `Reconcile`. Add the reaper goroutine `reapLoop(ctx, interval)` — scans for unsettled leases past `ttl_seconds` and settles them at the granted amount (pessimistic). Enforce per-agent `max_active_leases`. |
| `internal/wallet/wallet_test.go` | Extend — grant/deny across all three scopes; reaper idempotency; late-`Settle`-after-reap → `success: true` with `noop` ([RFC §F](0023-llm-call-leasing.md#f-failure-modes)); lease-ID collision rejection; `max_active_leases` cap. |
| `config/optimization.yaml` | New `wallet:` block — `ttl_seconds`, `reaper_interval_seconds`, `max_active_leases` (defaults per [RFC §B](0023-llm-call-leasing.md#b-lease-lifecycle) and [OQ #2](0023-llm-call-leasing.md#open-questions)). |
| `schemas/optimization.schema.json` | Add the `wallet` block schema (all keys typed; `additionalProperties: false`). |

#### Key implementation details

- The reaper interval default is 5 s; the TTL default follows the [OQ #2](0023-llm-call-leasing.md#open-questions) recommendation (`2 × max per-call timeout`, capped at 120 s). Both are config-tunable, not constants.
- `Release` is `Settle` with `actual_*_tokens = 0` — it reverses the provisional. The late-settle-after-reap case is monotone-safe per [RFC §F](0023-llm-call-leasing.md#f-failure-modes): the reaper-applied charge stands; `Settle` returns `success: true` with a `noop` indicator.
- Lock granularity is a single coarse mutex — acceptable for v0.3.x per [RFC §D](0023-llm-call-leasing.md#d-go-wallet-service); a `Reserve`-style `TokenCounter` API is the documented refactor if profiling later shows contention.
- The reaper goroutine `reapLoop` must carry its own `defer`/`recover` panic guard — a gRPC server interceptor only wraps RPC-handler goroutines, not background goroutines, so an unrecovered reaper panic would crash the orchestrator. See [ISSUE-0059](../issues/ISSUE-0059-grpc-server-no-panic-recovery-interceptor.md). The broader pre-existing gap that issue also tracked — the agent-facing gRPC server registering no panic-recovery interceptor, unlike the HTTP server's `recoveryMiddleware` — landed standalone in [#379](https://github.com/mkhomutov/Persatrix/pull/379) (ISSUE-0059 piece 1), so PR 2 carries only the `reapLoop` guard (piece 2): the server interceptor cannot cover a background goroutine.

#### Tests

- Deny path: an estimate that exceeds any one of the three scopes returns `LeaseDenied` with the correct `scope` and the `spent` / `limit` fields populated.
- Concurrent `AcquireLease` calls cannot both pass `CheckBudget` and both provision past the limit (mutex atomicity).
- Reaper settles an unsettled lease at the granted amount within `ttl + reaper_interval`; re-running the reaper over an already-settled lease is a no-op.
- `max_active_leases + 1` acquisitions for one agent → the last is rejected.

#### PR checklist

- [ ] `go test ./internal/wallet/... ./internal/cost/...` passes; `golangci-lint` clean.
- [ ] `RecordProvisional` / `Reconcile` on `TokenCounter`; `CheckBudget` composed by the wallet.
- [ ] Reaper settles crashed leases at the granted amount; idempotent.
- [ ] `reapLoop` carries a `defer`/`recover` panic guard ([ISSUE-0059](../issues/ISSUE-0059-grpc-server-no-panic-recovery-interceptor.md)).
- [ ] `make validate` passes against the new `wallet:` config block.
- [ ] No agent-side wiring — `agents/` untouched (PR 3).

---

### PR 3: `feature/v032-rfc0023-workflow-path` — `WalletClient` + Workflow-Task Lease Wiring

**Depends on**: PR 2 merged.
**Purpose**: Introduce the Python `WalletClient` and wrap `LLMClient.create_message` in the lease context manager for the workflow-task origin (`cause = WORKFLOW_TASK`). The pre-dispatch `CheckBudget` is kept as the early-fail optimisation. Implements [RFC §Phased Implementation Plan Phase 3](0023-llm-call-leasing.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `agents/wallet_client.py` | **New** — gRPC client wrapping the generated stub; exposes the single `lease(...)` async context manager per [RFC §E](0023-llm-call-leasing.md#e-python-client-integration): acquire on enter, settle with actual usage on normal exit, release on exception before the LLM call, settle-at-granted on exception after it. `acquire` must also branch on the wallet's non-`LeaseDenied` gRPC status rejections (`ResourceExhausted` / `Internal` / `InvalidArgument`) — see Key implementation details. |
| `agents/llm_client.py` | Wrap the existing `create_message` body in `async with wallet.lease(...) as lease:`; on success call `lease.settle(input_tokens=..., output_tokens=...)`. Propagate `persatrix.lease_id` as a span attribute. New typed `BudgetExceededError` raised on `LeaseDenied`. Input-token estimate uses the tokeniser path fixed by [OQ #5](0023-llm-call-leasing.md#open-questions). |
| `agents/server.py` *(or agent entry point)* | [OQ #1](0023-llm-call-leasing.md#open-questions) resolved (outbound dial) — reuse the gRPC channel the agent already opens for `LogService` (`--orchestrator-grpc` target), add the wallet stub on it, and a fail-closed boot condition (the LLM-call path does not start without an established wallet connection). No new `orchestrator_endpoint` config field — `--orchestrator-grpc` is reused. |
| `internal/scheduler/stage_runner.go` | Comment only — `CheckBudget` is now an early-fail optimisation, not the enforcement point ([RFC §G](0023-llm-call-leasing.md#g-migration-of-existing-checkbudget)). |
| `internal/scheduler/budget.go` | Retire the post-hoc `TokenCounter.RecordUsage` call in `recordStepUsage` — once this PR brackets the workflow-task LLM call in `wallet.lease(...)` the wallet records that spend on the same counter, so the scheduler's call double-counts it (Key implementation details below). The sibling `costReporter.RecordStepCost` call stays. |
| Executor surface (`internal/executor/` / agent task path) | Surface `BudgetExceededError` from a workflow task as `TaskStatus.FAILED` with a structured `error_message`. |
| `agents/tests/test_wallet_client.py` | **New** — `lease()` happy path, exception-before-call → release, exception-after-call → settle-at-granted, retry on transient settle failure (Python: failing pytest first, `LLMClient` mocked at the boundary, no real network). |
| `tests/integration/` | **New** — over-budget workflow run: `max_daily_usd` set so the *second* LLM call inside a single task is denied; assert the task fails with a structured `BudgetError` and the denied call's provisional charge is reversed. |
| `docs/observability.md` | Document the `persatrix.lease_id` span attribute and the new wallet metrics. |

#### Key implementation details

- `lease()` is the only public surface of `WalletClient` — callers never touch acquire/settle directly, so "every call path brackets its LLM call" is enforced by the context-manager shape.
- The pre-dispatch `CheckBudget` is **kept** ([RFC §G](0023-llm-call-leasing.md#g-migration-of-existing-checkbudget)) — it preserves fast-fail for clearly over-budget workflows. PR 3 only re-labels it; the agent-side per-call lease is now the enforcement point.
- `estimated_max_output_tokens` reuses the `max_tokens` value `create_message` already passes the provider — no new plumbing.
- **Wallet availability is coupled to the orchestrator log buffer.** PR 1 registers `WalletService` on the agent-facing gRPC listener that `cmd/orchestrator/main.go` only stands up inside `if logBuf != nil`. That coupling is inert for PRs 1–2 (no wallet clients), but this PR makes the wallet load-bearing: a failed log-buffer init then silently means no LLM call can acquire a lease. PR 3 must make this failure mode explicit rather than emergent — the agent-side fail-closed boot condition (the `agents/server.py` scope row above) is the enforcement backstop, and [RFC §F Failure Modes](0023-llm-call-leasing.md#f-failure-modes) is the documented home for the wallet-unreachable behaviour.
- **The scheduler's post-hoc `recordStepUsage` double-counts workflow-task spend once the wallet records it.** `internal/scheduler/budget.go`'s `recordStepUsage` writes a completed workflow step's actual token usage to the shared `TokenCounter` via `RecordUsage`. This PR brackets the workflow-task LLM call in `wallet.lease(...)`, and the wallet already writes that same spend to that counter — `RecordProvisional` at acquire, `Reconcile` to the provider actuals at settle. Left as-is, both paths count the workflow-task spend, inflating every budget scope ~2×. PR 3 retires the `RecordUsage` call from `recordStepUsage`; the wallet's `Reconcile` becomes the single recording authority for leased calls. The sibling `costReporter.RecordStepCost` call is independent — it feeds the per-step `/cost` endpoint, not the budget counter — and stays. Surfaced by the PR 2 review as a forward-looking note. **Caveat (PR 3 review):** this retirement is correct only while every LLM call contributing to a workflow step's reported token total is leased — true for `TaskAgent` (whose sole origin is the now-leased `_run_llm_loop`), but **not** for a workflow step dispatched to a persona agent, which routes through the un-leased persona action loop. That gap is tracked in [ISSUE-0063](../issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md) — confirm-or-lease before relying on the counter for persona-served workflow steps.
- **`AcquireLease` / `SettleLease` signal exceptional rejections as gRPC status errors, not only the in-band oneof.** A budget denial is the in-band `LeaseResponse_Denied` arm, but the wallet returns a gRPC status error for three non-budget rejections: `codes.ResourceExhausted` when the agent is at its per-agent active-lease cap, `codes.Internal` on a server-side lease-id collision, and `codes.InvalidArgument` when a request carries a negative or out-of-range token count. `WalletClient.lease(...)` must branch on all of them, not only the `LeaseDenied` arm: `LeaseDenied` maps to the typed `BudgetExceededError`; a `ResourceExhausted` status is *transient* — a cap slot frees as the agent's other leases settle — so it warrants a brief backoff-and-retry, surfaced distinctly from a hard budget failure rather than collapsed into `BudgetExceededError`; `Internal` / `InvalidArgument` indicate a server- or agent-side bug and should fail loudly. Surfaced by the PR 2 review as a forward-looking note.

#### Tests

- `test_wallet_client.py`: all four `lease()` exit paths (settle / release / settle-at-granted / retry).
- Integration: a two-LLM-call task with a budget that admits the first call and denies the second → task `FAILED`, structured `error_message`, second call's provisional reversed.

#### PR checklist

- [ ] `pytest agents/tests/test_wallet_client.py tests/integration/ -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] `LLMClient.create_message` wrapped in `wallet.lease(...)`; `BudgetExceededError` typed and raised on `LeaseDenied`.
- [ ] Workflow-task leases tagged `cause = WORKFLOW_TASK`.
- [ ] Pre-dispatch `CheckBudget` retained and re-labelled as early-fail.
- [ ] `persatrix.lease_id` span attribute documented in `docs/observability.md`.

---

### PR 4: `feature/v032-rfc0023-chat-path` — Chat-Path Lease Wiring

**Depends on**: PR 3 merged (`WalletClient` available).
**Purpose**: Wire the chat path through the wallet (`cause = CHAT`). Closes the v0.2.3 documented bypass — chat traffic now passes `BudgetEnforcer`. Implements [RFC §Phased Implementation Plan Phase 4](0023-llm-call-leasing.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `agents/server_servicers.py` | Surface `BudgetExceededError` on the chat path as `ChatResponse.reply_status = "error"` carrying the wallet's `LeaseDenied.message` ([RFC §E](0023-llm-call-leasing.md#e-python-client-integration)). |
| Chat call site (`agents/` chat handler) | The chat LLM call acquires a lease with `cause = CHAT`. |
| `agents/tests/` | Unit coverage — chat `BudgetExceededError` → `reply_status="error"` with the denied-lease message. |
| `tests/integration/` | **New** — chat session that exceeds the `per_agent` budget mid-conversation: assert `reply_status="error"`; assert subsequent chats are also denied until the budget resets. |
| `docs/manual-tests/MT-COST-003.md` | **New** — chat budget-exceed manual test ([RFC §Test Strategy](0023-llm-call-leasing.md#test-strategy)). Executed in [v0.3.2-plan Phase 4 PR 1](../v0.3.2-plan.md#phase-4--v032-release-prep-execution); follows the existing `docs/manual-tests/` structural template. |

#### Key implementation details

- The chat UX regression is accepted by [RFC §F](0023-llm-call-leasing.md#f-failure-modes): a transient wallet outage mid-conversation now surfaces as `reply_status="error"` rather than silently spending. `MT-COST-003` verifies the denied-lease path surfaces as a structured error, not a crash.
- The **README known-limitations line** on chat bypassing `BudgetEnforcer` is *not* deleted here — that edit is a v0.3.2 release-prep deliverable ([v0.3.2-plan Phase 4 PR 2](../v0.3.2-plan.md#phase-4--v032-release-prep-execution)). This PR closes the bypass; release-prep records it.

#### Tests

- Unit: chat handler raising `BudgetExceededError` → `ChatResponse.reply_status == "error"`, message echoes `LeaseDenied.message`.
- Integration: per-agent budget exhausted mid-chat → `reply_status="error"` on the over-budget turn and every turn after, until reset.

#### PR checklist

- [ ] `pytest agents/tests/ tests/integration/ -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] Chat leases tagged `cause = CHAT`; `BudgetExceededError` surfaces as `reply_status="error"`.
- [ ] `docs/manual-tests/MT-COST-003.md` authored; execution deferred to v0.3.2-plan Phase 4 PR 1.

---

### PR 5: `feature/v032-rfc0023-tick-subagent` — Autonomous TICK + Sub-Agent Lease Wiring

**Depends on**: PR 3 merged (`WalletClient` available); PR 4 recommended-before per the [recommended merge order](#overview).
**Purpose**: Wire the autonomous TICK loop (`cause = AUTONOMOUS_TICK`) and sub-agent spawns (`cause = SUB_AGENT`) through the wallet. Implements [RFC §Phased Implementation Plan Phase 5](0023-llm-call-leasing.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| `agents/persona_runtime/action_loop.py` | The autonomous TICK LLM call acquires a lease with `cause = AUTONOMOUS_TICK`. On `BudgetExceededError`, the loop treats the tick as idle — log a warning, increment `idle_count` consistent with the v0.2.2 short-circuit. Record budget-throttled idle ticks with a distinct `idle_reason = budget_denied` attribute on the existing TICK metric ([RFC §Phase 5](0023-llm-call-leasing.md#phased-implementation-plan)), distinguishable from `empty_context_tick` and natural-idle ticks. |
| `agents/sub_agents/` | Sub-agent spawn LLM calls acquire leases with `cause = SUB_AGENT`, attributed against the **parent's** `agent_id` so spend lands on the originating persona ([RFC §Phase 5](0023-llm-call-leasing.md#phased-implementation-plan)). The `max_active_leases` cap stays per-process per [OQ #7](0023-llm-call-leasing.md#open-questions). |
| `agents/persona_runtime/action_loop.py` (persona `TASK_ASSIGNED` path) | **[ISSUE-0063](../issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md)** — a workflow step dispatched to a *persona* agent routes through the persona action loop, not the leased `_run_llm_loop`; its LLM spend is recorded by neither a wallet lease nor (since PR 3 retired the `recordStepUsage` counter feed) the budget `TokenCounter`. Close it here: pass `cause = CAUSE_WORKFLOW_TASK` on the persona `TASK_ASSIGNED` path, or add a planner/scheduler guard rejecting persona-agent step targets. |
| `agents/tests/` | Unit coverage — TICK `BudgetExceededError` → idle tick, `idle_count` increments, `idle_reason=budget_denied`; sub-agent lease attributed to the parent `agent_id`; the chosen ISSUE-0063 resolution (persona workflow-task lease, or guard rejecting persona step targets). |
| `tests/integration/` | **New** — persona TICK loop with the `per_agent` budget exhausted: assert ticks are recorded idle, no LLM calls reach the provider, `idle_count` increments. |
| `docs/manual-tests/MT-COST-004.md` | **New** — TICK budget exhaustion → idle manual test ([RFC §Test Strategy](0023-llm-call-leasing.md#test-strategy)). Executed in [v0.3.2-plan Phase 4 PR 1](../v0.3.2-plan.md#phase-4--v032-release-prep-execution). |
| `docs/observability.md` | Add the `idle_reason=budget_denied` TICK-metric attribute to the wallet-metrics section started in PR 3. |

#### Key implementation details

- The TICK idle treatment is consistent with the v0.2.2 empty-context short-circuit — a denied lease is *not* a crash; the tick advances `idle_count` and the loop continues.
- `idle_reason=budget_denied` is the dashboard discriminator: without it, sustained budget pressure is invisible against organic quiet periods ([RFC §Phase 5](0023-llm-call-leasing.md#phased-implementation-plan)).
- Sub-agent spend attribution (parent) is split from the concurrency cap (per-process) per the [OQ #7](0023-llm-call-leasing.md#open-questions) resolution — the cap is a DoS ceiling, attribution is a separate concern.
- **[ISSUE-0063](../issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md) closes in this PR.** PR 3 retired the scheduler's post-hoc `recordStepUsage` counter feed assuming every workflow-step LLM call is leased — true for `TaskAgent`, but not for a persona agent serving a workflow step (un-leased action loop). The gap is latent today (no shipped workflow routes a step to a persona agent) but unguarded. PR 5 already edits `action_loop.py` for the TICK origin, so it is the natural home: decide during PR 5 design between leasing the persona `TASK_ASSIGNED` path and a planner-side step-target guard.

#### Tests

- Unit: TICK `BudgetExceededError` → `idle_count` increments, metric carries `idle_reason=budget_denied`.
- Unit: sub-agent lease `agent_id` equals the parent persona's `agent_id`.
- Integration: budget-exhausted TICK loop → zero provider calls, idle ticks recorded.

#### PR checklist

- [ ] `pytest agents/tests/ tests/integration/ -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] TICK leases tagged `AUTONOMOUS_TICK`; sub-agent leases tagged `SUB_AGENT` against the parent `agent_id`.
- [ ] Budget-denied idle ticks carry `idle_reason=budget_denied`.
- [ ] [ISSUE-0063](../issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md) resolved — persona-served workflow steps either acquire a `CAUSE_WORKFLOW_TASK` lease or are rejected by a planner/scheduler guard.
- [ ] `docs/manual-tests/MT-COST-004.md` authored; execution deferred to v0.3.2-plan Phase 4 PR 1.

---

### PR 6: `feature/v032-rfc0023-channel-message` — Channel-Message Origin Lease Wiring

**Depends on**: PR 3 merged (`WalletClient` available); PR 5 recommended-before per the [recommended merge order](#overview).
**Purpose**: Wire the RFC 0011 channel-message reply path through the wallet (`cause = CHANNEL_MESSAGE`). Implements [RFC §Phased Implementation Plan Phase 6](0023-llm-call-leasing.md#phased-implementation-plan).

#### Scope

| File | Change |
|------|--------|
| Channel-message reply call site (`agents/` channel-message handler) | When a channel delivers a message and the recipient agent generates an LLM-backed reply, the reply LLM call acquires a lease with `cause = CHANNEL_MESSAGE`. |
| `agents/tests/` | Unit coverage — channel-message reply acquires a `CHANNEL_MESSAGE`-tagged lease; the [RFC 0011 response gate](0011-pr-plan.md) runs *before* lease acquisition (no lease held during gate evaluation). |
| `tests/integration/` | **New** — channel-message delivery → positive gate decision → lease acquired → reply; and a budget-denied variant where the lease is refused after a positive gate decision. |

#### Key implementation details

- The RFC 0011 response gate (already shipped) is evaluated **first** — only a positive gate decision leads to a lease acquisition, so no lease is held during gate evaluation ([RFC §Phase 6](0023-llm-call-leasing.md#phased-implementation-plan)).
- This is the fifth and last LLM-call origin; after PR 6 every origin in [RFC §Goal #1](0023-llm-call-leasing.md#goals) acquires a server-issued lease — the [v0.3.2 acceptance gate](../v0.3.2-plan.md#acceptance-for-v032).

#### Tests

- Unit: a channel-message reply path acquires exactly one `CHANNEL_MESSAGE` lease; the gate decision precedes acquisition.
- Integration: positive-gate channel reply succeeds with a lease; budget-denied channel reply surfaces the denial without crashing the recipient agent.

#### PR checklist

- [ ] `pytest agents/tests/ tests/integration/ -q` passes.
- [ ] `ruff check agents/` clean; `mypy agents/` clean.
- [ ] Channel-message replies tagged `cause = CHANNEL_MESSAGE`; response gate precedes lease acquisition.
- [ ] All five LLM-call origins now acquire a lease (v0.3.2 acceptance).

---

### PR 7: `feature/v032-rfc0023-followups` — Review Follow-Ups

**Depends on**: PR 6 merged (all six implementation PRs complete).
**Purpose**: Address review findings surfaced during PRs 1–6. Follows the [RFC 0017 PR plan §PR 6 precedent](0017-pr-plan.md) — "From PR N review" subsections, each finding paraphrased inline.

#### Scope

Items below are populated as PRs are reviewed. Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) ("Local-only files MUST NEVER be referenced in any committed file"), each entry paraphrases the finding inline and does **not** reference or link any local PR review report.

##### From PR 1 review

_PR 1 review surfaced no work deferred to this PR. The lone coverage gap — the `NewWalletService` nil-logger fallback was unexercised by any test — was closed within PR 1 with a constructor test. The orchestrator-side wallet-availability coupling (`WalletService` registers only inside `cmd/orchestrator/main.go`'s `if logBuf != nil` block) was folded into PR 3's [Key implementation details](#pr-3-featurev032-rfc0023-workflow-path--walletclient--workflow-task-lease-wiring) rather than deferred here._

##### From PR 2 review

_The first PR 2 review surfaced four findings, all addressed within PR 2 rather than deferred here. The wallet config loader (`internal/wallet/config.go`) now rejects an explicit zero or negative tuning value instead of silently substituting the default — its raw fields became `*int` so an omitted key (use the default) is distinguishable from an explicit `0` (rejected), aligning the loader with the `minimum: 1` bound `schemas/optimization.schema.json` already enforces at `make validate`. `NewWalletService` now panics on a nil `TokenCounter` / `BudgetEnforcer`, matching the `NewCostReporter` / `NewLogServiceServer` nil-required-dependency convention. `cmd/orchestrator/main.go` loads the wallet config only when the cost config loaded — a missing `optimization.yaml` no longer logs a misleading "wallet using defaults" line when no `WalletService` is constructed. The reaper's purge-window comment was corrected to describe the issue-time-keyed horizon the code actually applies._

_A second, deeper PR 2 review surfaced one blocking issue and three minor findings; the blocker and two minors are addressed within PR 2, the remaining minor and three nits are deferred here. **Blocker — flaky test.** The live-goroutine reaper test (`internal/wallet/wallet_reaper_test.go`) asserted the lease's transient settled-and-still-present map state — a state the reaper itself destroys ~one TTL later when it purges the lease — so its poll could consistently sample either side of that narrow window and never observe it, failing reproducibly under `-race`. It now asserts a terminal, purge-immune signal: the reaper's one-shot "lease reaped" log record, captured via a `zaptest/observer` core. **Minors fixed in PR 2.** `NewWalletService` now also panics on an unusable zero-value `Config{}` — a non-positive `TTL`, `ReaperInterval`, or `MaxActiveLeases` — closing the asymmetry with the nil-dependency guard above: a zero-value `Config` otherwise denies every lease and panics the reaper's `time.NewTicker`. And the `TokenCounter.RecordProvisional` doc comment, which framed a colliding-`leaseID` overwrite as a harmless defensive measure, was corrected — the overwrite would in fact leak the first charge into the scope totals; re-recording is instead made unreachable by the WalletService's collision guard, which the comment now states as a load-bearing caller guarantee._

_A third PR 2 review surfaced one minor finding, fixed within PR 2, and one forward-looking note, folded into PR 3 rather than deferred here. **Minor fixed in PR 2.** The `lease` struct carried `workflowID` and `model` fields that `AcquireLease` set but no code ever read — dead state. Rather than drop them, PR 2 wires both into the reaper's one-shot "lease reaped" WARN record, which previously logged only `lease_id` / `agent_id` / `cause`: a reaped lease is an abnormal lifecycle event — an agent left a lease unsettled past its TTL — and an operator triaging it needs the workflow and model in that same always-on record, since the "lease granted" line that also carries them is DEBUG. **Folded into PR 3.** Once PR 3 brackets the workflow-task LLM call in `wallet.lease(...)`, the wallet's provisional/reconcile and the scheduler's existing post-hoc `recordStepUsage` → `TokenCounter.RecordUsage` would both write the same workflow-task spend to the counter — a ~2× double-count. This is recorded as a PR 3 scope row plus a Key-implementation-detail bullet (retire the `RecordUsage` call) rather than deferred here, following the PR 1 review precedent of folding future-PR implementation guidance into that PR's section._

_A fourth PR 2 review surfaced one minor finding and one test gap, both addressed within PR 2 rather than deferred here. **Minor — `int32` overflow guard, pulled forward.** The unbounded `ttl_seconds` that overflows the `int32` `LeaseGrant.ttl_seconds` advertised on the wire — item (1) of the second review's deferred list below — was closed within PR 2 rather than left for this PR: `schemas/optimization.schema.json` introduces the `wallet:` block in PR 2 itself, so shipping it with a `minimum` but no `maximum` and patching it five PRs later was avoidable churn. Each of the three `wallet` keys gained an inclusive `maximum` in the schema — `ttl_seconds` 86400 (1 day), `reaper_interval_seconds` 3600 (1 hour), `max_active_leases` 1024 (64× the default) — fat-finger guards an order of magnitude past any real deployment, and `internal/wallet/config.go`'s `LoadConfig` gained the matching upper-bound rejection, preserving the schema↔loader agreement its doc comment already promises. The `maxTTLSeconds` cap also makes the `int32(cfg.TTL.Seconds())` narrowing in `AcquireLease` provably non-overflowing. **Test gap filled.** The reaper's reconcile-miss branch — a lease whose provisional was cleared by `ResetDaily` while still in flight, then expired by the reaper — had no test; one was added pinning that the reaper still marks such a lease settled (so it is not re-reaped on every pass), logs the miss for the operator, and resurrects no spend onto the reset counter._

_A fifth PR 2 review surfaced one should-fix finding, addressed within PR 2 rather than deferred here, plus one forward-looking note folded into PR 3. **Should-fix — unvalidated agent-supplied token counts.** `AcquireLease` and `SettleLease` fed the request's `int64` token fields straight into the cost math with no sign or range check. The negative case was the bypass: `cost.EstimateCost` is unclamped arithmetic, so a negative `estimated_input_tokens` / `estimated_max_output_tokens` yields a negative charge that `RecordProvisional` *subtracts* from every budget scope — silently manufacturing headroom — and, if the lease is then abandoned, the reaper's settle-at-granted reconciles the negative estimate against itself (delta ≈ 0), making that negative charge permanent: a standing enforcement hole in the very PR that adds enforcement. A negative `actual_*_tokens` on `SettleLease` corrupts the scope totals the same way; the mirror case (an absurd count, or an `int64` overflow of the summed estimate) lets one malformed lease exhaust every scope. PR 2 closes this at the RPC boundary — a new `validateTokenCount` rejects any token field outside `[0, maxTokenCount]` (1e9, ~500× the largest production context window: a fat-finger / pre-auth guard that also keeps the budget-check sum clear of `int64` overflow) with `codes.InvalidArgument`, on `AcquireLease`'s two estimates and `SettleLease`'s two actuals. The `cost` primitives stay pure arithmetic — they are shared with the trusted scheduler `RecordUsage` path — so the guard lives in the wallet, consistent with its existing unknown-lease / collision / per-agent-cap defensive posture. **Folded into PR 3.** `AcquireLease` / `SettleLease` now reject some requests with a gRPC status error rather than the in-band oneof — `codes.ResourceExhausted` (per-agent cap), `codes.Internal` (lease-id collision), and the new `codes.InvalidArgument` (malformed token count); PR 3's `WalletClient` must branch on all of them, recorded as a PR 3 Key-implementation-detail bullet below rather than deferred here._

_Deferred to this PR from the second PR 2 review (a third item from that review — the `int32` narrowing of `ttl_seconds` — was pulled forward and closed within PR 2; see the fourth PR 2 review paragraph above): (1) the `finalize` log messages are assembled by string concatenation (`"wallet: " + op + " rejected …"`) — move the `settle` / `release` discriminator into a `zap.String("op", op)` field so the messages are constants log aggregators can group on; (2) the wallet's lease-lifecycle defaults are duplicated between the JSON-schema `default:` values and the Go constants in `internal/wallet/config.go` — they agree today but nothing keeps them in sync (drift risk noted; no mechanism warranted). One related code-clarity nit needs no separate tracking — in `cmd/orchestrator/main.go` `walletSvc` is constructed whenever the cost config loaded but only registered and given a running reaper inside the nested `grpcServer != nil` → `logBuf != nil` block, so a log-buffer init failure builds a wallet that is never served; the load-bearing half of this (wallet enforcement silently disabled when the log buffer fails) is already tracked as a PR 3 implementation detail, and the remainder is a one-line comment best folded into that PR 3 change._

##### From PR 3 review

_None recorded at plan-authoring time._

##### From PR 4 review

_None recorded at plan-authoring time._

##### From PR 5 review

_PR 5 review surfaced one finding deferred here as a tracked issue, plus one
test-readability cleanup applied inline within PR 5._

_**Deferred — persona-as-sub-agent attribution gap.** The
`CAUSE_SUB_AGENT` cause / parent-agent attribution PR 5 wires lives only in
`BaseAgent._run_llm_loop` (`agents/base.py`). A `PersonaAgent` dispatched as a
sub-agent child does not use that path — `PersonaAgent.handle` wraps the task
as a `TASK_ASSIGNED` event and routes through
`agents/persona_runtime/action_loop.py`, whose `cause_for_event` returns
`CAUSE_WORKFLOW_TASK` and uses `self.agent_id` for lease attribution. The
`task.config.sub_agent_parent_id` field the spawner threads is silently
ignored on the persona path, so the child's spend would bill the child rather
than the delegating parent. Structural twin of [ISSUE-0063](../issues/ISSUE-0063-workflow-step-unleased-llm-spend-uncounted.md)
(persona action loop being a parallel LLM-call origin to `_run_llm_loop` that
needs its own copy of any lease-attribution overrides). **Latent today** —
`SPAWN_SUB_AGENT` returns `{"status": "not_implemented"}` in
`agents/action_executor.py:170-189` and `SubAgentSpawner` has no production
caller (integration-test fixtures only). Tracked under
[ISSUE-0064](../issues/ISSUE-0064-persona-as-sub-agent-attribution-gap.md);
recommended resolution path is the same lease-in-the-action-loop route
ISSUE-0063 took, folded into whichever PR re-opens the persona action loop
next (RFC 0023 PR 6's channel-message wiring is the natural candidate)._

_**Applied inline — test-readability cleanup.** Two assertions in
`agents/tests/test_action_loop_tick_lease.py` used
`actions == [actions[0]] and actions[0].action_type == DO_NOTHING` to assert
"exactly one DO_NOTHING action". The comparison is correct (true iff
`len(actions) == 1`) but awkward; replaced with the idiomatic
`len(actions) == 1` + per-element assertion. No behavior change._

##### From PR 6 review

_None recorded at plan-authoring time._

#### PR checklist

- [ ] All deferred review findings addressed or downgraded to tracked issues with rationale.
- [ ] `make test` + `make lint` clean.
- [ ] Deferred test gaps from PRs 1–6 reviews filled.

---

### PR 8: `feature/v032-rfc0023-close` — Full-RFC Closeout

**Depends on**: PR 7 merged.
**Purpose**: Mark RFC 0023 implemented. Full-RFC closeout — all six phases shipped.

#### Scope

| File | Change |
|------|--------|
| [`docs/rfcs/0023-llm-call-leasing.md`](0023-llm-call-leasing.md) | Status → `✅ Implemented`. Append an "Implemented in v0.3.2" note to Decision/Next Steps. |
| [`ROADMAP.md`](../../ROADMAP.md) | RFC 0023 row → `✅ Implemented`; merged-PR rows for PRs 1–8; `Last updated` refresh. |
| [`docs/rfcs/0023-pr-plan.md`](0023-pr-plan.md) | [Progress Overview](#progress-overview) rows filled with merged-PR numbers and dates; all checklists complete. |

No code changes; doc-only. `CHANGELOG.md` is **deferred to the v0.3.2 release process** ([v0.3.2-plan Phase 3 / 4](../v0.3.2-plan.md#phase-3--v032-release-prep-plan)), mirroring the [RFC 0017 PR 7 precedent](0017-pr-plan.md).

#### PR checklist

- [ ] RFC 0023 status = `✅ Implemented`.
- [ ] [ROADMAP RFC Master Index](../../ROADMAP.md#rfc-master-index) updated; merged-PR history includes PRs 1–8.
- [ ] [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 2 → ✅ Merged.
- [ ] `make test`, `make lint`, `make validate` pass (doc-only change confirms no regression).

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| [OQ #1](0023-llm-call-leasing.md#open-questions) (transport) and [OQ #5](0023-llm-call-leasing.md#open-questions) (tokeniser parity) were non-additive once the proto contract ships in PR 1. | **Resolved 2026-05-18** — see [§Phase 0 Hard Gate](#phase-0-hard-gate) and [RFC 0023 §Open Questions](0023-llm-call-leasing.md#open-questions): outbound dial reusing the `LogService` channel, and `tiktoken` promoted to a hard runtime dependency. The hard gate is cleared; PR 1 may open. |
| The wallet fails closed by design ([RFC §F](0023-llm-call-leasing.md#f-failure-modes)) — a wallet outage now breaks live chat that previously bypassed enforcement. | The chat UX regression is explicitly accepted in [RFC §F](0023-llm-call-leasing.md#f-failure-modes). `MT-COST-003` (PR 4) exercises the denied-lease path and verifies it surfaces as a structured `reply_status="error"`, not a crash. |
| The per-LLM-call gRPC round-trip adds latency; exceeding the ≤ 5 ms p99 budget ([RFC §Goal #6](0023-llm-call-leasing.md#goals)) makes the cost gate a latency regression. | The [RFC §Test Strategy](0023-llm-call-leasing.md#test-strategy) loopback load test measures p99 acquire+settle. Informational, not a build gate; recorded in the [v0.3.2 Phase 4 MT report](../v0.3.2-plan.md#phase-4--v032-release-prep-execution) so a regression is visible at release review. |
| Six call-site PRs (3–6, plus 1–2) risk drifting the proto contract if a later PR needs a field the skeleton did not reserve. | PR 1 ships the full `Cause` enum and the complete message set from [RFC §C](0023-llm-call-leasing.md#c-proto-surface); PRs 2–6 build against the frozen contract. Any contract change after PR 1 is a flagged review event, not a silent edit. |
| A reaper bug could free spend (settle a live call as crashed too early) or leak provisional holds (never reap). | TTL defaults to `2 × per-call timeout` ([OQ #2](0023-llm-call-leasing.md#open-questions)); PR 2's reaper tests pin idempotency and the late-settle-after-reap monotone-safe path ([RFC §F](0023-llm-call-leasing.md#f-failure-modes)). |
| This plan rots as PRs 1–6 land. | Each PR's checklist updates the [Progress Overview](#progress-overview) and the [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview); the [ROADMAP Hygiene](#roadmap-hygiene) rules below are part of every PR. |

---

## ROADMAP Hygiene

Per [.github/copilot-instructions.md §Status Hygiene](../../.github/copilot-instructions.md) and [v0.3.2-plan §ROADMAP hygiene](../v0.3.2-plan.md#roadmap-hygiene):

- **This PR-plan PR opens / merges** → no RFC 0023 status change — authoring a PR plan does not start implementation; RFC 0023 stays `📋 Proposed`. The [RFC Master Index](../../ROADMAP.md#rfc-master-index) *target* flips to `v0.3.2` in this PR.
- **PR 1 opens** → RFC 0023 row → `🚧 Implementing` (first implementation PR); [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 2 → 🔄 In progress.
- **Each PR merges** → fill the [Progress Overview](#progress-overview) row with the PR number and date.
- **PR 8 merges** → RFC 0023 row → `✅ Implemented`; [v0.3.2-plan Master Progress Overview](../v0.3.2-plan.md#master-progress-overview) row 2 → ✅ Merged; `Last updated` refresh.

---

## Progress Overview

| # | RFC Phase | Title | Branch | Status | GitHub PR | Merged |
|---|-----------|-------|--------|--------|-----------|--------|
| 1 | 1 | Proto surface + wallet skeleton | `feature/v032-rfc0023-proto-skeleton` | 🔀 PR open | [#378](https://github.com/mkhomutov/Persatrix/pull/378) | — |
| 2 | 2 | Real enforcement + reaper | `feature/v032-rfc0023-wallet-enforcement` | 🔀 PR open | [#384](https://github.com/mkhomutov/Persatrix/pull/384) | — |
| 3 | 3 | `WalletClient` + workflow-task wiring | `feature/v032-rfc0023-workflow-path` | 🔀 PR open | [#385](https://github.com/mkhomutov/Persatrix/pull/385) | — |
| 4 | 4 | Chat-path wiring (closes the v0.2.3 bypass) | `feature/v032-rfc0023-chat-path` | 🔀 PR open | [#387](https://github.com/mkhomutov/Persatrix/pull/387) | — |
| 5 | 5 | Autonomous TICK + sub-agent wiring | `feature/v032-rfc0023-tick-subagent` | 🔀 PR open | — | — |
| 6 | 6 | Channel-message origin wiring | `feature/v032-rfc0023-channel-message` | ⬜ Not started | — | — |
| 7 | — | Review follow-ups | `feature/v032-rfc0023-followups` | ⬜ Not started | — | — |
| 8 | — | Full-RFC closeout | `feature/v032-rfc0023-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [RFC 0023 — LLM Call Leasing](0023-llm-call-leasing.md) — canonical spec.
- [v0.3.2-plan.md](../v0.3.2-plan.md) — master plan; row 2 of the Master Progress Overview is this workstream.
- [RFC 0017 PR plan](0017-pr-plan.md) — structural template for this plan (review-follow-ups + RFC-close PR shape).
- [RFC 0006 — Efficiency & Execution Limits](0006-efficiency-execution-limits.md) — the per-task budget shape this RFC complements.
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) / [RFC 0011 PR plan](0011-pr-plan.md) — the channel-message origin and response gate PR 6 wires into.
- [RFC 0029 PR plan](0029-pr-plan.md) — the paired v0.3.2 workstream (disjoint surface — memory facade, not the LLM-call path).
- [`internal/cost/cost.go`](../../internal/cost/cost.go) — `BudgetEnforcer` and `TokenCounter`, composed by the new `WalletService`.
- `docs/manual-tests/MT-COST-003.md`, `docs/manual-tests/MT-COST-004.md` — authored in PRs 4 / 5, executed in [v0.3.2-plan Phase 4 PR 1](../v0.3.2-plan.md#phase-4--v032-release-prep-execution).
