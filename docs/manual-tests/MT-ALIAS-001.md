# Manual Test MT-ALIAS-001: Alias-Routed Agent Reports Correctly-Keyed Cost (live)

**Test ID**: `MT-ALIAS-001`
**Feature Area**: Cost / Model Alias (RFC 0033)
**Version**: 1.0
**Created**: 2026-05-27
**Last Updated**: 2026-05-27
**Status**: Active

> **v0.3.4 recipe — config-driven (no default provider).** The base
> [`config/optimization.yaml`](../../config/optimization.yaml) ships the `quality` / `fast` /
> `summarizer` aliases **unconfigured** — there is no default provider, so a plain
> `docker compose up` fails loud at agent startup. This test runs `make demo-anthropic`, which
> mounts [`config/demo/anthropic/optimization.yaml`](../../config/demo/anthropic/optimization.yaml)
> (pointing `quality` → `anthropic` / `claude-sonnet-4-6`, priced 3.00 / 15.00) over the stack's
> config ([amendment 2026-05-27](../v0.3.4-plan-amendment-2026-05-27.md)). Re-run live against the
> config-driven HEAD (see [Test Results](#test-results)).

---

## Overview

**Purpose**: Verify the primary v0.3.4 user-facing promise — *agents reference models by
logical alias, and cost attribution survives the indirection*. An agent whose `model:` is the
`quality` alias must resolve to the physical vendor ID (`claude-sonnet-4-6`), complete a real
turn, and report **non-zero, correctly-keyed** cost via `GET /api/v1/cost/summary`. The
`agent.llm.call` span must carry `persatrix.llm.model_alias=quality` while
`gen_ai.request.model` stays the physical ID — the alias is a telemetry-only annotation,
never forwarded to the provider.

**Scope**: The RFC 0033 §D resolver path through
[`agents/llm_factory.py`](../../agents/llm_factory.py) `create_provider()` → the alias-derived
pricing table ([§F](../rfcs/0033-model-alias-layer.md#f-pricing-keyed-by-alias)) → the Go cost
pipeline (`internal/cost`, keyed by physical model ID) → the `model_alias` span attribute
([§G](../rfcs/0033-model-alias-layer.md#g-telemetry)). This is the human-driven companion to the
automated `cost-attribution gate` (`internal/server/cost_alias_gate_test.go` +
`internal/cost/cost_alias_pricing_test.go`).

**Out of Scope**: The raw-ID fall-through path (`MT-MEMORY-003` keeps its raw
`claude-haiku-4-5` references; the §E deprecation warning is a separate concern); the one-line
**provider swap** (that is [MT-ALIAS-002](MT-ALIAS-002.md)); offline / Ollama routing
([MT-OFFLINE-001](MT-OFFLINE-001.md) / [MT-OLLAMA-001](MT-OLLAMA-001.md)).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0033-model-alias-layer.md](../rfcs/0033-model-alias-layer.md) — §D (resolver
  integration point), §F (pricing keyed by alias), §G (telemetry / `model_alias` span attr).
- [config/optimization.yaml](../../config/optimization.yaml) — the `models.aliases` block
  (`quality` → `anthropic` / `claude-sonnet-4-6`, priced) and the derived
  `cost.pricing.models` table.
- [agents/model_aliases.py](../../agents/model_aliases.py) — `resolve()` (alias → physical ID).
- [agents/llm_factory.py](../../agents/llm_factory.py) — `create_provider()` (returns the
  physical model ID; threads the alias into the span only).

**Related Automated Tests**:
- Go (release-blocker gate): `internal/server/cost_alias_gate_test.go`
  (`TestCostSummary_AliasRoutedAgent_ReportsNonZeroCost`),
  `internal/cost/cost_alias_pricing_test.go`
  (`TestLoadCostConfig_ShippedConfig_PricesAliasPhysicalModels`).
- Python: `tests/unit/python/test_model_aliases.py` (alias hit / raw fall-through / warning).

**Related Manual Tests**:
- [MT-ALIAS-002](MT-ALIAS-002.md) — one-line provider swap (the headline claim).
- [MT-COST-001](MT-COST-001.md) — the cost-summary endpoint contract this test reuses.
- [MT-IDLE-001](MT-IDLE-001.md) — the v0.3.3 cost gate carried forward.

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Go 1.24+, Python 3.11+, Docker + Docker Compose
- `curl` + `jq` (or `python3 -m json.tool`) in PATH
- A real `ANTHROPIC_API_KEY` (the `quality` alias resolves to Anthropic).

### Application State

- ☐ Stack up via `make demo-anthropic`
  (`docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up -d --build` —
  orchestrator + agents + collector + jaeger + prometheus), all services healthy. The overlay
  mounts [`config/demo/anthropic/optimization.yaml`](../../config/demo/anthropic/optimization.yaml),
  which configures `quality` → `anthropic` / `claude-sonnet-4-6` (priced `3.00` / `15.00`).
- ☐ `make validate` exits 0 (base config valid; `schema_version: "0.2"`).
- ☐ The base `config/optimization.yaml` ships `quality` **unconfigured** (no default provider) — a
  plain `docker compose up` (no demo overlay) fails loud at agent startup with the actionable
  "pick a provider" `SystemExit`. The Anthropic configuration arrives via the mounted demo config,
  not the base file.

> **Operator note (carry-forward F-3)**: an *empty* `ANTHROPIC_API_KEY` exported in the shell
> shadows the populated `.env` under Compose interpolation precedence. v0.3.4 plumbs keys with a
> `:-` empty default (the hard `:?` guard was dropped), so an empty shell value no longer aborts
> `up` — the agent just logs an unset-key warning and fails on the first request. Unset the empty
> shell var (or give it the real key) before `docker compose` so Compose reads `.env`.

### Test Data

The stock `ember-owl` persona (and every task agent) already references `model: "quality"` in
[`config/agents.yaml`](../../config/agents.yaml) — no agent edit is needed. `make demo-anthropic`
supplies the `quality` alias's provider/model/pricing via the mounted demo config.

---

## Test Procedure

### Step 1: Confirm the Agent Resolves the Alias to the Physical Model

**Action**: Inspect the agent startup logs for the resolver decision — and confirm **no**
raw-ID deprecation warning fires (the agent is alias-routed, not on the §E pass-through).

```bash
docker logs persatrix-agent-ember-owl-1 2>&1 | grep -iE "DEPRECATION .RFC 0033.|model|provider" | head
```

**Expected Result**: The agent boots an `AnthropicProvider` for the physical `claude-sonnet-4-6`.
**No** `DEPRECATION (RFC 0033): agent … references a raw vendor model ID` line appears (that line
fires only for raw-ID agents).

**Verification**:
- [ ] Agent started with the Anthropic provider on the physical model
- [ ] No RFC 0033 raw-ID deprecation warning for this agent

---

### Step 2: Capture a Cost Baseline

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/alias-cost-before.json | python3 -m json.tool
```

**Expected Result**: A cost-summary snapshot. Note `daily_input_tokens`, `daily_output_tokens`,
`daily_estimated_usd`, and the `top_agents` per-agent breakdown.

**Verification**:
- [ ] Baseline snapshot captured

---

### Step 3: Drive One Real Turn Through the Alias-Routed Agent

**Action**:

```bash
curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/ember-owl/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"In one sentence, what is your top priority this sprint?","user_id":"local"}' \
  | jq '{reply_status, reply}'
```

**Expected Result**: HTTP 200, `reply_status="ok"`, non-empty `reply`. The turn ran a real
Anthropic call against the alias-resolved physical model.

**Verification**:
- [ ] `reply_status="ok"` with a non-empty reply

---

### Step 4: Confirm Non-Zero, Correctly-Keyed Cost

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/alias-cost-after.json | python3 -m json.tool
```

**Expected Result**: `daily_input_tokens` / `daily_output_tokens` / `daily_estimated_usd`
**advanced from the baseline** and `daily_estimated_usd` is **strictly > 0** — proving the cost
pipeline priced the physical `claude-sonnet-4-6` the alias resolved to (the derived pricing
table). The `top_agents` breakdown attributes the spend to `ember-owl`. A $0 reading here would
mean pricing was keyed to a model the alias does **not** resolve to (a migration mis-key) — the
exact failure the alias-derived pricing table prevents.

**Verification**:
- [ ] `daily_estimated_usd` strictly greater than 0
- [ ] Tokens advanced from the Step 2 baseline
- [ ] Spend attributed to `ember-owl` in `top_agents`

---

### Step 5: Confirm the `model_alias` Span Attribute (alias annotates, physical ID routes)

**Action**: In the Jaeger UI (`http://127.0.0.1:16686`) find the latest `agent.llm.call` span
for `ember-owl` (or query the collector), and inspect its attributes.

**Expected Result**: The span carries **both**:
- `gen_ai.request.model = claude-sonnet-4-6` (the physical ID sent to the vendor), and
- `persatrix.llm.model_alias = quality` (the logical alias, telemetry-only).

The alias is added *alongside* the physical model, never substituted for it, and is never
forwarded to the provider API.

**Verification**:
- [ ] `gen_ai.request.model` is the physical `claude-sonnet-4-6`
- [ ] `persatrix.llm.model_alias` equals `quality`

---

### Step 6: Confirm the Automated Cost-Attribution Gate

**Action**:

```bash
go test ./internal/server/ -run 'AliasRoutedAgent_ReportsNonZeroCost' -count=1 -v
go test ./internal/cost/  -run 'PricesAliasPhysicalModels'           -count=1 -v
```

**Expected Result**: Both pass. They are the automated release-blocker counterpart to this MT —
`cost_alias_gate_test.go` asserts an alias-routed agent yields non-zero correctly-keyed cost;
`cost_alias_pricing_test.go` asserts the shipped `cost.pricing.models` table is the projection
of the alias map (no stale hand-edit, no missing physical model).

**Verification**:
- [ ] `TestCostSummary_AliasRoutedAgent_ReportsNonZeroCost` PASS
- [ ] `TestLoadCostConfig_ShippedConfig_PricesAliasPhysicalModels` PASS

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Agent resolves `quality` → `claude-sonnet-4-6`; no raw-ID warning | ☐ |
| 2 | Cost baseline captured | ☐ |
| 3 | One real turn returns `reply_status="ok"` | ☐ |
| 4 | `daily_estimated_usd > 0`, tokens advanced, keyed to `ember-owl` | ☐ |
| 5 | Span carries physical `gen_ai.request.model` + `model_alias=quality` | ☐ |
| 6 | Both automated cost-attribution gate tests pass | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Alias Resolves to a Model Absent From the Pricing Table

**Scenario**: An alias's physical model has no entry in `cost.pricing.models`.

**Expected Behavior**: `EstimateCost` returns $0 and the RFC 0023 budget gate is silently
disabled — the migration mis-key this test guards against. The
`TestLoadCostConfig_ShippedConfig_PricesAliasPhysicalModels` gate fails closed at CI, and PR 4's
missing-price guard (`agents/model_aliases.py`) fails closed for an unpriced **non-local** alias.

### Edge Case 2: Explicit `provider:` Field Disagrees With the Alias

**Scenario**: An agent sets `model: "quality"` and also `provider: openai`.

**Expected Behavior**: `create_provider()` raises `SystemExit` (§D rule 1) — the alias is
authoritative; the redundant disagreeing field is a config bug, not a silent resolve-one-way.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker Desktop | ✅ Pass | See [`v0.3.4-execution-report.md`](v0.3.4-execution-report.md#mt-alias-001--primary-gate-evidence-live) for per-step evidence. |
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker 29.3.1 | ✅ Pass | **Config-driven re-run on HEAD `6ce23cd`** (`make demo-anthropic`): no raw-ID / key warning; chat turn `reply_status="ok"`; cost `2708/20/$0.008424`, **exactly** the `claude-sonnet-4-6` rate (2708×$3.00/1M + 20×$15.00/1M), keyed to ember-owl; Prometheus `agent_llm_calls_total{gen_ai_system="anthropic", gen_ai_request_model="claude-sonnet-4-6"}=1`. Both Go gate tests PASS on HEAD. |

---

## Notes

- The cost summary and `docker logs` are lag-free signals; the OTEL counters export on the
  metric-reader interval and lag by up to one interval, so use them only as corroboration.
- This is the v0.3.4 analogue of v0.3.3's `MT-IDLE-001` gate: there the promise was *idle costs
  nothing*; here it is *an alias-routed turn costs the right amount, keyed correctly*.
