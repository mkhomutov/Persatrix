# Manual Test MT-OFFLINE-001: Offline Mode — Full Round-Trip at $0, Zero Network

**Test ID**: `MT-OFFLINE-001`
**Feature Area**: Provider / Offline (MockProvider)
**Version**: 1.0
**Created**: 2026-05-27
**Last Updated**: 2026-05-27
**Status**: Active

---

## Overview

**Purpose**: Verify the keyless, zero-cost offline mode — *the whole society runs with no API
key, no network egress, and no spend*. With `PERSATRIX_OFFLINE=1` (the `make demo-offline` knob),
every agent routes to the in-process `MockProvider`, serving curated replies from
[`config/offline_responses.yaml`](../../config/offline_responses.yaml). A full chat round-trip
must complete, populate the `gen_ai.*` spans, and settle the RFC 0023 wallet lease at **$0**.

**Scope**: The offline force-flag path through
[`agents/llm_factory.py`](../../agents/llm_factory.py) `create_provider()` (offline wins over
every other selector, before the `model:` field is read) →
[`agents/llm_offline.py`](../../agents/llm_offline.py) `MockProvider` → the same span / wallet
machinery a real provider uses, but with $0 pricing. Carried from
[#422](https://github.com/mkhomutov/Persatrix/pull/422).

**Out of Scope**: Real local inference (that is [MT-OLLAMA-001](MT-OLLAMA-001.md) — Ollama serves
a real model); cloud-provider routing and alias swaps ([MT-ALIAS-001](MT-ALIAS-001.md) /
[MT-ALIAS-002](MT-ALIAS-002.md)).

---

## Related Documentation

**Feature Documentation**:
- [docker-compose.offline.yaml](../../docker-compose.offline.yaml) — the overlay
  (`PERSATRIX_OFFLINE=1` + `PERSATRIX_OFFLINE_RESPONSES` per agent).
- [Makefile](../../Makefile) `demo-offline` target.
- [agents/llm_offline.py](../../agents/llm_offline.py) — `MockProvider`, `offline_mode_enabled()`.
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
- ☐ **No API key required.** `make demo-offline` passes a throwaway
  `ANTHROPIC_API_KEY=offline-not-used` only to satisfy the base compose file's `:?` key-guard;
  it is never used (offline routing precedes any provider SDK construction).
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

**Expected Result**: The stack builds and comes up healthy with `PERSATRIX_OFFLINE=1` per agent.
Agent logs show `LLM offline mode active for agent '<id>' — using MockProvider (no API calls, no
cost)`.

**Verification**:
- [ ] All agents healthy
- [ ] `LLM offline mode active … MockProvider` logged for each agent

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

### Edge Case 2: Offline + Ollama Both Set

**Scenario**: Both `PERSATRIX_OFFLINE=1` and `PERSATRIX_OLLAMA=1` are exported.

**Expected Behavior**: Offline wins (it needs neither a network nor a running daemon) — the
factory checks offline first. Covered by `test_llm_offline.py`.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker Desktop | ✅ Pass | See [`v0.3.4-execution-report.md`](v0.3.4-execution-report.md#mt-offline-001--offline-0-evidence-live). |

---

## Notes

- The throwaway `ANTHROPIC_API_KEY` in `make demo-offline` only satisfies the base compose `:?`
  guard, which Compose evaluates before the overlay merges; it is never consumed.
- Offline mode is the "$0 keyless demo" promise; Ollama is the "$0 real-model" promise. Both sit
  on the same provider-selection axis (`create_provider`).
</content>
