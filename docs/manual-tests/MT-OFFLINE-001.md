# Manual Test MT-OFFLINE-001: Offline Mode — Full Round-Trip at $0, Zero Network

**Test ID**: `MT-OFFLINE-001`
**Feature Area**: Provider / Offline (MockProvider)
**Version**: 1.0
**Created**: 2026-05-27
**Last Updated**: 2026-05-27
**Status**: Active

> **v0.3.4 recipe — config-driven (no force-knob).** Provider selection is purely
> config/alias-driven: `make demo-offline` selects the mock provider by mounting
> [`config/demo/offline/optimization.yaml`](../../config/demo/offline/optimization.yaml)
> (every alias → `provider: mock`) over the stack's `optimization.yaml` via a Compose
> overlay — there is **no** `PERSATRIX_OFFLINE` force-knob
> ([amendment 2026-05-27](../v0.3.4-plan-amendment-2026-05-27.md)). The steps below use
> that config-driven recipe and were re-run live against the config-driven HEAD
> (see [Test Results](#test-results)).

---

## Overview

**Purpose**: Verify the keyless, zero-cost offline mode — *the whole society runs with no API
key, no network egress, and no spend*. With `make demo-offline` (which mounts an alias config
pointing every alias at `provider: mock`), every agent routes to the in-process `MockProvider`,
serving curated replies from
[`config/offline_responses.yaml`](../../config/offline_responses.yaml). A full chat round-trip
must complete, populate the `gen_ai.*` spans, and settle the RFC 0023 wallet lease at **$0**.

**Scope**: The `provider: mock` path through
[`agents/llm_factory.py`](../../agents/llm_factory.py) `create_provider()` — the resolved alias's
`provider` field selects the concrete class, the *same standard path as every provider* (RFC 0033
§D) →
[`agents/llm_offline.py`](../../agents/llm_offline.py) `MockProvider` → the same span / wallet
machinery a real provider uses, but with $0 pricing. Carried from
[#422](https://github.com/mkhomutov/Persatrix/pull/422).

**Out of Scope**: Real local inference (that is [MT-OLLAMA-001](MT-OLLAMA-001.md) — Ollama serves
a real model); cloud-provider routing and alias swaps ([MT-ALIAS-001](MT-ALIAS-001.md) /
[MT-ALIAS-002](MT-ALIAS-002.md)).

---

## Related Documentation

**Feature Documentation**:
- [docker-compose.offline.yaml](../../docker-compose.offline.yaml) — the overlay (mounts
  `config/demo/offline/optimization.yaml` + sets `PERSATRIX_OFFLINE_RESPONSES` per agent — that
  env var is mock *configuration*, where to read replies, not a provider-selection knob).
- [config/demo/offline/optimization.yaml](../../config/demo/offline/optimization.yaml) — the mock
  alias config the overlay mounts (`quality` / `fast` / `summarizer` → `provider: mock`).
- [Makefile](../../Makefile) `demo-offline` target.
- [agents/llm_factory.py](../../agents/llm_factory.py) — `create_provider()` (the `provider: mock`
  branch).
- [agents/llm_offline.py](../../agents/llm_offline.py) — `MockProvider`, `MockProvider.from_config()`.
- [config/offline_responses.yaml](../../config/offline_responses.yaml) — curated per-agent
  replies + persona-flavoured fallback.

**Related Automated Tests**:
- Python: `tests/unit/python/test_llm_offline.py` (MockProvider + factory-interplay regression —
  offline wins over Ollama and over the `model:` field).

**Related Manual Tests**:
- [MT-OLLAMA-001](MT-OLLAMA-001.md) — the real-local-model sibling.
- [MT-IDLE-001](MT-IDLE-001.md) — the idle-cost gate (also a $0 assertion, different path).

---

## Preconditions

### System Requirements

- ☐ Windows / macOS / Linux with Docker + Docker Compose
- ☐ **No API key required.** The base compose plumbs every provider key with a `:-` empty default
  (the single-vendor `:?` startup guard was dropped in v0.3.4), and the mock provider constructs no
  SDK and makes no network call — so no key, throwaway or real, is needed.
- ☐ `curl` + `jq` in PATH.

### Application State

- ☐ `make demo-offline` brings up the stack with the offline overlay; all agents healthy.
- ☐ (Optional, to prove zero egress) a way to observe network — e.g. confirm the agent
  containers make no outbound connection to `api.anthropic.com` / `api.openai.com` during a turn.

### Test Data

The curated replies in `config/offline_responses.yaml` (keyword-matched, with a graceful
generated fallback for unmatched turns). No setup needed.

---

## Test Procedure

### Step 1: Bring Up the Offline Society

**Action**:

```bash
make demo-offline
docker compose -f docker-compose.yaml -f docker-compose.offline.yaml ps
```

**Expected Result**: The stack builds and comes up healthy with the mock alias config mounted.
Agent logs show `Offline mock provider active for agent '<id>' — using MockProvider (no API calls,
no cost)`.

**Verification**:
- [ ] All agents healthy
- [ ] `Offline mock provider active … MockProvider` logged for each agent

---

### Step 2: Capture a Cost Baseline

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/offline-cost-before.json | python3 -m json.tool
```

**Verification**:
- [ ] Baseline captured (expected `0/0/$0` on a fresh stack)

---

### Step 3: Drive a Full Chat Round-Trip

**Action**:

```bash
curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/ember-owl/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"We have a flaky test in CI. What do you want to do?","user_id":"local"}' \
  | jq '{reply_status, reply}'
```

**Expected Result**: HTTP 200, `reply_status="ok"`, a non-empty curated reply (the `["flaky"]`
keyword match for `ember-owl`). The round-trip completed entirely in-process.

**Verification**:
- [ ] `reply_status="ok"` with a non-empty reply
- [ ] Reply is the curated offline text (not a real-LLM generation)

---

### Step 4: Confirm Zero Spend and Zero Egress

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/offline-cost-after.json | python3 -m json.tool
diff <(jq -S . /tmp/offline-cost-before.json) <(jq -S . /tmp/offline-cost-after.json) && echo "NO COST CHANGE"
```

**Expected Result**: `daily_estimated_usd` is unchanged at **$0** after the turn. No agent made
an outbound provider call. The wallet lease (if the turn is on a leased origin) settled at $0.

**Verification**:
- [ ] `daily_estimated_usd` unchanged at $0
- [ ] No outbound connection to a cloud provider during the turn

---

### Step 5: Confirm Populated `gen_ai.*` Spans

**Action**: Inspect the latest `agent.llm.call` span for `ember-owl` (Jaeger
`http://127.0.0.1:16686`).

**Expected Result**: The span is present with populated `gen_ai.*` attributes — the offline path
goes through the same span machinery, so observability is intact even with no provider call.
`gen_ai.usage.*_tokens` reflect the mock's reported usage; the cost stays $0.

**Verification**:
- [ ] `agent.llm.call` span present with `gen_ai.*` attributes populated

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Offline society up; `MockProvider` active per agent | ☐ |
| 2 | Cost baseline captured | ☐ |
| 3 | Full chat round-trip returns curated reply, `reply_status="ok"` | ☐ |
| 4 | $0 spend after the turn; zero cloud egress | ☐ |
| 5 | `gen_ai.*` spans populated despite no provider call | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Unmatched Message

**Scenario**: A chat message matches no curated keyword set for the agent.

**Expected Behavior**: The turn degrades gracefully to a generated persona-flavoured placeholder
reply — still `reply_status="ok"`, still $0.

### Edge Case 2: A Real Provider Key Is Present in the Environment

**Scenario**: A real `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is exported while the mock alias
config is mounted.

**Expected Behavior**: Ignored — provider selection is the resolved alias `provider` (here
`mock`), not key presence. The factory constructs `MockProvider`, builds no provider SDK, and
makes no network call. Covered by `test_llm_offline.py` (the factory-interplay regression: an
alias declaring `provider: mock` routes to the mock regardless of the environment).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker Desktop | ✅ Pass | See [`v0.3.4-execution-report.md`](v0.3.4-execution-report.md#mt-offline-001--offline-0-evidence-live). |
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker 29.3.1 | ✅ Pass | **Config-driven re-run on HEAD `6ce23cd`** (`make demo-offline`): log `Offline mock provider active for agent 'ember-owl' — using MockProvider (no API calls, no cost)`; chat turn `reply_status="ok"` with the curated `["flaky"]` reply; cost `1600/146/$0` keyed to ember-owl; Prometheus `agent_llm_calls_total{gen_ai_system="mock", gen_ai_request_model="offline"}=1`. |

---

## Notes

- `make demo-offline` needs no key: the base compose plumbs provider keys with a `:-` empty
  default (the single-vendor `:?` startup guard was dropped in v0.3.4), and the mock provider
  builds no SDK.
- Offline mode is the "$0 keyless demo" promise; Ollama is the "$0 real-model" promise. Both sit
  on the same provider-selection axis (the resolved alias `provider` in `create_provider`).
