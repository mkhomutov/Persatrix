# Manual Test MT-ALIAS-002: One-Line Provider Swap (headline claim)

**Test ID**: `MT-ALIAS-002`
**Feature Area**: Model Alias / Provider parity (RFC 0033)
**Version**: 1.0
**Created**: 2026-05-27
**Last Updated**: 2026-05-27
**Status**: Active

---

## Overview

**Purpose**: Validate the v0.3.4 headline claim — *"a provider swap is a one-line edit."* The
same agent, unchanged, must route to a **different provider** after editing a single field on
its model alias in [`config/optimization.yaml`](../../config/optimization.yaml), and still report
correct cost (non-zero for a priced cloud peer; documented-$0 for a local target).

**Scope**: The RFC 0033 §D promise that model identity lives in **one** place. We flip the
`quality` alias's `provider` / `model` from the Anthropic default to a priced OpenAI peer
(`gpt-4o`) — a one-line-class edit to the alias entry, with **no edit** to `config/agents.yaml`,
the routing defaults, or any agent code — and confirm the same `ember-owl` persona now calls
OpenAI with correctly-keyed non-zero cost. The local-target variant (Edge Case 1) flips to the
`ollama` provider and documents the $0-local case.

**Out of Scope**: First-time alias→cost wiring (that is [MT-ALIAS-001](MT-ALIAS-001.md)); the
keyless local/offline demo paths ([MT-OFFLINE-001](MT-OFFLINE-001.md) /
[MT-OLLAMA-001](MT-OLLAMA-001.md) — which, like every demo since the v0.3.4
[knob-free refactor](../v0.3.4-plan-amendment-2026-05-27.md), also select their provider via a
mounted alias config; this test exercises the *user-facing* one-line edit to the live
`config/optimization.yaml` rather than a pre-baked demo overlay).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0033-model-alias-layer.md](../rfcs/0033-model-alias-layer.md) — §Motivation
  (one-line migration), §D (alias is authoritative).
- [docs/v0.3.4-plan-amendment-2026-05-24.md](../v0.3.4-plan-amendment-2026-05-24.md) — Phase 4
  additions, item 3 (the provider-swap MT).
- [config/optimization.yaml](../../config/optimization.yaml) — `models.aliases.quality` (the
  edited entry) and `models.aliases.quality-openai` (the priced OpenAI peer that demonstrates a
  $0-trip-free swap target).

**Related Automated Tests**:
- Python: `tests/unit/python/test_model_aliases.py` (resolver returns the alias's declared
  provider); `tests/unit/python/test_optimization_cost_pricing.py`
  (`TestShippedCostPricingDerivedFromAliases` — the derived pricing table covers every alias's
  physical model, so a swapped target is already priced).

**Related Manual Tests**:
- [MT-ALIAS-001](MT-ALIAS-001.md) — the alias-routing cost gate (run first).
- [MT-OLLAMA-001](MT-OLLAMA-001.md) — the local-model path the Edge Case 1 swap mirrors.

---

## Preconditions

### System Requirements

- ☐ Windows / macOS / Linux with Docker + Docker Compose
- ☐ A real `OPENAI_API_KEY` (the swap target resolves to OpenAI `gpt-4o`). For the local
  variant (Edge Case 1) a running Ollama daemon instead — no key.
- ☐ `curl` + `jq` in PATH.

### Application State

- ☐ Full stack up and healthy (as [MT-ALIAS-001](MT-ALIAS-001.md) Preconditions).
- ☐ `make validate` exits 0 before and after the edit.
- ☐ Working tree clean — the alias edit is **reverted** at the end of the test
  (`git checkout config/optimization.yaml`).

### Test Data

The stock `quality` alias resolves to `anthropic` / `claude-sonnet-4-6`. The shipped
`quality-openai` alias resolves to `openai` / `gpt-4o` (priced, `2.50` / `10.00`) and is the
reference target. This test edits the **`quality`** entry so the unchanged `ember-owl` (which
references `quality`) follows the swap without touching the agent.

---

## Test Procedure

### Step 1: Baseline — Confirm the Agent Routes to Anthropic

**Action**: Drive one turn on the stock config and confirm the provider (per
[MT-ALIAS-001](MT-ALIAS-001.md) Steps 3–5): `gen_ai.system=anthropic`,
`gen_ai.request.model=claude-sonnet-4-6`, `model_alias=quality`.

**Verification**:
- [ ] Turn routes to Anthropic on `claude-sonnet-4-6`

---

### Step 2: The One-Line Swap

**Action**: Edit **only** the `quality` alias's `provider` + `model` (and its inline price to the
swap target's list price) in `config/optimization.yaml`:

```yaml
  aliases:
    quality:
      provider: openai          # was: anthropic
      model: gpt-4o             # was: claude-sonnet-4-6
      input_per_1m_tokens: 2.50 # was: 3.00
      output_per_1m_tokens: 10.00 # was: 15.00
```

Regenerate the derived Go pricing table if the swap introduces a physical model not already in
`cost.pricing.models` (here `gpt-4o` is already present from the `quality-openai` peer, so the
table is unchanged), run `make validate`, and recreate the agent containers so they re-resolve:

```bash
make validate
docker compose -f docker-compose.yaml up -d --force-recreate agent-ember-owl
```

**Expected Result**: `make validate` exits 0. **No edit was made to `config/agents.yaml`, the
routing defaults, or any agent code** — the swap is the alias edit alone.

**Verification**:
- [ ] Only the `quality` alias entry changed (`git diff --stat` shows one file)
- [ ] `make validate` exits 0

---

### Step 3: Confirm the Same Agent Now Routes to OpenAI

**Action**: Re-run the same chat turn from Step 1, unchanged:

```bash
curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/ember-owl/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"In one sentence, what is your top priority this sprint?","user_id":"local"}' \
  | jq '{reply_status, reply}'
```

Then inspect the latest `agent.llm.call` span for `ember-owl`.

**Expected Result**: HTTP 200, `reply_status="ok"`. The span now shows `gen_ai.system=openai`,
`gen_ai.request.model=gpt-4o`, and still `persatrix.llm.model_alias=quality` — the **same
agent**, the **same alias name**, a **different provider**.

**Verification**:
- [ ] `reply_status="ok"`
- [ ] `gen_ai.system=openai`, `gen_ai.request.model=gpt-4o`
- [ ] `persatrix.llm.model_alias` still `quality`

---

### Step 4: Confirm Cost Re-Keyed to the New Physical Model

**Action**: Capture `GET /api/v1/cost/summary` before and after the Step 3 turn.

**Expected Result**: `daily_estimated_usd` advanced by a non-zero amount priced at the **gpt-4o**
rate (`2.50` / `10.00` per 1M), keyed to `ember-owl`. Cost followed the swap because the pricing
table is derived from the alias map — not hand-keyed to the old physical model.

**Verification**:
- [ ] `daily_estimated_usd` advanced, strictly > 0
- [ ] Spend attributed to `ember-owl`

---

### Step 5: Revert

**Action**:

```bash
git checkout config/optimization.yaml
make validate
docker compose -f docker-compose.yaml up -d --force-recreate agent-ember-owl
git diff --quiet && echo "TREE CLEAN"
```

**Verification**:
- [ ] `config/optimization.yaml` restored; working tree clean

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Stock agent routes to Anthropic `claude-sonnet-4-6` | ☐ |
| 2 | One-line alias edit only; `make validate` clean | ☐ |
| 3 | Same agent now routes to OpenAI `gpt-4o`; alias name unchanged | ☐ |
| 4 | Cost re-keyed to gpt-4o rate, non-zero, keyed to `ember-owl` | ☐ |
| 5 | Config reverted; working tree clean | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Swap to a Local Provider (documented $0)

**Scenario**: Flip `quality` to `provider: ollama` / `model: llama3.2` (a local model, $0 real
cost) with an explicit `0` price.

**Expected Behavior**: The same agent routes to the local Ollama daemon; `gen_ai.system=ollama`,
real token counts, and `daily_estimated_usd` advances by **$0** — correct, because a $0-real
local model carries an explicit `0` price (it is *$0-by-design*, distinguished from a
forgot-the-price omission by the missing-price guard). This is the documented-$0 case the
headline claim must also cover.

### Edge Case 2: Swap Target Missing From the Pricing Table

**Scenario**: Swap `quality` to a priced cloud model whose physical ID is **not** in
`cost.pricing.models`.

**Expected Behavior**: PR 4's missing-price guard fails closed (`SystemExit`) for the unpriced
non-local alias, and `TestShippedCostPricingDerivedFromAliases` fails at CI — so the swap can't
ship with cost silently disabled. Regenerate the derived table from the alias map as part of the
swap.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker Desktop | ✅ Pass | See [`v0.3.4-execution-report.md`](v0.3.4-execution-report.md#mt-alias-002--one-line-provider-swap-evidence-live). |

---

## Notes

- "One-line edit" is shorthand for *one alias entry*: provider + model (+ its inline price). The
  point is that the edit is confined to the alias block — agents, routing defaults, pricing
  table, and code are untouched.
- Keep the swap reverted in version control; this MT mutates `config/optimization.yaml` only for
  the duration of the run.
