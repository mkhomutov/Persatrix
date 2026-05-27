# Manual Test MT-ALIAS-002: One-Line Provider Swap (headline claim)

**Test ID**: `MT-ALIAS-002`
**Feature Area**: Model Alias / Provider parity (RFC 0033)
**Version**: 1.0
**Created**: 2026-05-27
**Last Updated**: 2026-05-27
**Status**: Active

> **v0.3.4 recipe — config-driven (no default provider; no `quality-openai` peer).** The base
> [`config/optimization.yaml`](../../config/optimization.yaml) ships every role alias
> **unconfigured**, and there is **no** shipped `quality-openai` peer alias — the per-provider
> configs live under [`config/demo/<provider>/`](../../config/demo/). This test demonstrates the
> one-line provider swap as the move from the Anthropic alias config to the OpenAI alias config:
> the **same agents**, unchanged, re-route to a different provider because the `quality` / `fast` /
> `summarizer` alias entries name a different `provider` / `model`
> ([amendment 2026-05-27](../v0.3.4-plan-amendment-2026-05-27.md)). Re-run live against HEAD (see
> [Test Results](#test-results)).

---

## Overview

**Purpose**: Validate the v0.3.4 headline claim — *"a provider swap is a one-line edit."* The
same agent, unchanged, must route to a **different provider** after re-pointing its model alias's
`provider` / `model`, and still report correct cost (non-zero for a priced cloud peer;
documented-$0 for a local target).

**Scope**: The RFC 0033 §D promise that model identity lives in **one** place. We re-point the
`quality` / `fast` / `summarizer` aliases from Anthropic to a priced OpenAI peer (`gpt-4o`) — an
alias-block change with **no edit** to `config/agents.yaml`, the routing defaults, or any agent
code — and confirm the same `ember-owl` persona now calls OpenAI with correctly-keyed non-zero
cost. In v0.3.4 each provider is a mounted alias config, so the swap is `make demo-anthropic` →
`make demo-openai` (the `config/demo/anthropic` vs `config/demo/openai` diff is exactly the alias
block); the equivalent manual form is editing the `quality` entry's `provider` / `model` / price in
the active config. The local-target variant (Edge Case 1) flips to the `ollama` provider and
documents the $0-local case.

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
  additions, item 3 (the provider-swap MT); and
  [amendment 2026-05-27](../v0.3.4-plan-amendment-2026-05-27.md) (config-driven, no default provider).
- [config/demo/anthropic/optimization.yaml](../../config/demo/anthropic/optimization.yaml) and
  [config/demo/openai/optimization.yaml](../../config/demo/openai/optimization.yaml) — the two
  alias configs whose `quality` / `fast` / `summarizer` block is the entire swap delta.

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

- ☐ `make demo-anthropic` up and healthy (the baseline, as [MT-ALIAS-001](MT-ALIAS-001.md)).
- ☐ `make validate` exits 0 (base config).
- ☐ If you demonstrate the swap by editing a config file in place (rather than switching demo
  overlays), the edit is **reverted** at the end (`git checkout <the edited config>`) and the
  working tree is clean. The demo-overlay swap mutates no tracked file.

### Test Data

The Anthropic alias config (`config/demo/anthropic`) resolves `quality` → `anthropic` /
`claude-sonnet-4-6` (priced `3.00` / `15.00`); the OpenAI alias config (`config/demo/openai`)
resolves `quality` → `openai` / `gpt-4o` (priced `2.50` / `10.00`). The unchanged `ember-owl`
(which references `quality`) follows whichever provider the active `quality` entry names — without
touching the agent, the routing defaults, or any code.

---

## Test Procedure

### Step 1: Baseline — Confirm the Agent Routes to Anthropic

**Action**: With `make demo-anthropic` up, drive one turn and confirm the provider (per
[MT-ALIAS-001](MT-ALIAS-001.md) Steps 3–5): `gen_ai.system=anthropic`,
`gen_ai.request.model=claude-sonnet-4-6`, `model_alias=quality`.

**Verification**:
- [ ] Turn routes to Anthropic on `claude-sonnet-4-6`

---

### Step 2: The One-Line Swap

**Action**: Re-point the society from Anthropic to OpenAI by switching to the OpenAI alias config —
the v0.3.4 config-driven form of the one-line swap:

```bash
make demo-openai
```

This mounts [`config/demo/openai/optimization.yaml`](../../config/demo/openai/optimization.yaml) in
place of the Anthropic one. The **only** thing that differs between the two configs is the alias
block (`quality` → `openai` / `gpt-4o`, priced `2.50` / `10.00`); `config/agents.yaml`, the routing
defaults, and all agent code are identical:

```bash
diff <(grep -A4 'quality:' config/demo/anthropic/optimization.yaml) \
     <(grep -A4 'quality:' config/demo/openai/optimization.yaml)
```

The equivalent **manual** one-line-class edit (without the demo overlay) is to flip the `quality`
entry's `provider` / `model` / price in the active `config/optimization.yaml`, then
`docker compose ... up -d --force-recreate agent-ember-owl`. The OpenAI physical model (`gpt-4o`) is
already priced in the derived `cost.pricing.models` table, so no table regeneration is needed.

**Expected Result**: The swap is confined to the alias block — **no edit** to `config/agents.yaml`,
the routing defaults, or any agent code. `gpt-4o` is already in the derived pricing table.

**Verification**:
- [ ] The swap changes only the alias `provider` / `model` / price (the `diff` above is the whole delta)
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

### Step 5: Revert / Teardown

**Action**: The overlay swap mutates no tracked file — switch back to the baseline (or tear down):

```bash
make demo-anthropic    # back on Anthropic
# or: make docker-down
git diff --quiet && echo "TREE CLEAN"
```

If you used the **manual** in-place edit instead, revert it
(`git checkout config/optimization.yaml` — or the edited demo config), `make validate`, and
`--force-recreate agent-ember-owl`.

**Verification**:
- [ ] Working tree clean (no tracked config left edited)

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Stock agent routes to Anthropic `claude-sonnet-4-6` | ☐ |
| 2 | Swap confined to the alias block (overlay switch or one-entry edit); `make validate` clean | ☐ |
| 3 | Same agent now routes to OpenAI `gpt-4o`; alias name unchanged | ☐ |
| 4 | Cost re-keyed to gpt-4o rate, non-zero, keyed to `ember-owl` | ☐ |
| 5 | Working tree clean (overlay leaves no tracked edit; revert any in-place edit) | ☐ |

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
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker 29.3.1 | ✅ Pass | **Config-driven re-run on HEAD `6ce23cd`**: baseline `make demo-anthropic` (`claude-sonnet-4-6`, `$0.008424`), swapped via `make demo-openai` — the same unchanged ember-owl now `reply_status="ok"` on OpenAI; cost `1887/17/$0.0048875`, **exactly** the `gpt-4o` rate (1887×$2.50/1M + 17×$10.00/1M; at the old sonnet rate it would have been $0.005916). Prometheus `agent_llm_calls_total{gen_ai_system="openai", gen_ai_request_model="gpt-4o"}=1`. `OPENAI_API_KEY` is now plumbed into agents by the base compose — the former F-5 throwaway-override gap is closed. |

---

## Notes

- "One-line edit" is shorthand for *one alias entry*: provider + model (+ its inline price). The
  point is that the swap is confined to the alias block — agents, routing defaults, and code are
  untouched (and `gpt-4o` is already in the derived pricing table).
- The demo-overlay swap mutates no tracked file. If you demonstrate the swap by editing a config in
  place instead, revert it (`git checkout`) so the working tree stays clean.
