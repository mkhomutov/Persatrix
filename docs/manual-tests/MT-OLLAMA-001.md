# Manual Test MT-OLLAMA-001: Ollama Local Model — Real Tokens, $0 Cloud Spend

**Test ID**: `MT-OLLAMA-001`
**Feature Area**: Provider / Ollama (local model)
**Version**: 1.0
**Created**: 2026-05-27
**Last Updated**: 2026-05-27
**Status**: Active

> **v0.3.4 recipe — config-driven (no force-knob).** Provider selection is purely
> config/alias-driven: `make demo-ollama` selects the Ollama provider by mounting
> [`config/demo/ollama/optimization.yaml`](../../config/demo/ollama/optimization.yaml)
> (every alias → `provider: ollama`, with the daemon `base_url`) over the stack's
> `optimization.yaml` — there is **no** `PERSATRIX_OLLAMA` force-knob
> ([amendment 2026-05-27](../v0.3.4-plan-amendment-2026-05-27.md)).
> `PERSATRIX_OLLAMA_MODEL` / `PERSATRIX_OLLAMA_BASE_URL` survive as provider
> *configuration* (not selection). The steps below use that config-driven recipe and
> were re-run live against the config-driven HEAD (see [Test Results](#test-results)).

---

## Overview

**Purpose**: Verify the keyless, free *real-model* path — *the whole society runs on a model
served locally by Ollama, with no API key and no per-token cloud spend*. With
`make demo-ollama` (which mounts an alias config pointing every alias at `provider: ollama`),
every agent routes to the `OllamaProvider` (a thin OpenAI-compatible subclass) pointed at the
bundled `ollama` service. A chat turn must complete with **real** token counts (a real model
generated the reply) while `daily_estimated_usd` stays at **$0** (local model is $0-real cost).

**Scope**: The `provider: ollama` path through
[`agents/llm_factory.py`](../../agents/llm_factory.py) `create_provider()` — the resolved alias's
`provider` field selects the concrete class (RFC 0033 §D), and the alias `model:` is the Ollama
tag (overridable per-agent by `PERSATRIX_OLLAMA_MODEL`) →
[`agents/llm_ollama.py`](../../agents/llm_ollama.py) `OllamaProvider` → the bundled `ollama`
service's OpenAI-compatible `/v1` endpoint. Carried from
[#423](https://github.com/mkhomutov/Persatrix/pull/423).

**Out of Scope**: Mock/curated replies (that is [MT-OFFLINE-001](MT-OFFLINE-001.md) — no real
inference); cloud-provider routing and alias swaps ([MT-ALIAS-001](MT-ALIAS-001.md) /
[MT-ALIAS-002](MT-ALIAS-002.md)). Reply *quality* is not asserted — a small local model is
expected to be weaker than a cloud model; this test checks routing, real tokens, and $0 cloud
spend, not answer quality.

---

## Related Documentation

**Feature Documentation**:
- [docker-compose.ollama.yaml](../../docker-compose.ollama.yaml) — the overlay (`ollama` service
  + one-shot `ollama-pull` gate; mounts `config/demo/ollama/optimization.yaml`;
  `PERSATRIX_OLLAMA_MODEL` / `PERSATRIX_OLLAMA_BASE_URL` per agent as provider *configuration*).
- [config/demo/ollama/optimization.yaml](../../config/demo/ollama/optimization.yaml) — the Ollama
  alias config the overlay mounts (`quality` / `fast` / `summarizer` → `provider: ollama`, with the
  daemon `base_url`).
- [Makefile](../../Makefile) `demo-ollama` target (default model `llama3.2`; override with
  `PERSATRIX_OLLAMA_MODEL`).
- [agents/llm_factory.py](../../agents/llm_factory.py) — `create_provider()` (the `provider: ollama`
  branch; applies the `PERSATRIX_OLLAMA_MODEL` override).
- [agents/llm_ollama.py](../../agents/llm_ollama.py) — `OllamaProvider`, `resolve_ollama_base_url()`.

**Related Automated Tests**:
- Python: `tests/unit/python/test_llm_ollama.py` (OllamaProvider + factory-interplay regression —
  Ollama force-flag, model substitution, offline-wins precedence).

**Related Manual Tests**:
- [MT-OFFLINE-001](MT-OFFLINE-001.md) — the curated-reply, no-inference sibling.

---

## Preconditions

### System Requirements

- ☐ Windows / macOS / Linux with Docker + Docker Compose
- ☐ **No API key required.** The base compose plumbs every provider key with a `:-` empty default
  (the single-vendor `:?` startup guard was dropped in v0.3.4), and Ollama ignores auth — so no key,
  throwaway or real, is needed. The mounted `provider: ollama` alias config routes every agent to
  the local daemon.
- ☐ Disk space for the model (the first `up` pulls a few GB into the `ollama-models` volume).
- ☐ `curl` + `jq` in PATH.

### Application State

- ☐ `make demo-ollama` brings up the `ollama` service, runs the one-shot `ollama-pull` (gated:
  agents wait for `service_completed_successfully`), and starts the agents routed to the local
  model.
- ☐ The pulled model is present: `docker exec persatrix-ollama-1 ollama list` shows the
  configured model.

### Test Data

None — the local model generates replies. The default model is `llama3.2`.

---

## Test Procedure

### Step 1: Bring Up the Local-Model Society

**Action**:

```bash
make demo-ollama
docker exec persatrix-ollama-1 ollama list
```

**Expected Result**: The `ollama` service is healthy, `ollama-pull` completed, and `ollama list`
shows the configured model present. The agents start cleanly with **no** raw-ID deprecation warning
and **no** `OPENAI_API_KEY not set` warning (the alias routes to `provider: ollama`, which needs no
key). **Note**: the `ollama` branch of `create_provider()` emits **no** provider-selection startup
log line (unlike the `mock` branch) — confirm routing in Step 4 via the `gen_ai.system=ollama`
telemetry, not a boot log.

**Verification**:
- [ ] Configured model present in `ollama list`
- [ ] Agents healthy with no raw-ID / key warning (routing is confirmed by telemetry in Step 4)

---

### Step 2: Capture a Cost Baseline

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/ollama-cost-before.json | python3 -m json.tool
```

**Verification**:
- [ ] Baseline captured (expected `0/0/$0` on a fresh stack)

---

### Step 3: Drive a Chat Round-Trip Through the Local Model

**Action**:

```bash
curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/ember-owl/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"Reply with exactly one short sentence: what is your sprint priority?","user_id":"local","timeout_seconds":280}' \
  | jq '{reply_status, reply}'
```

**Expected Result**: HTTP 200, `reply_status="ok"`, a non-empty reply **generated by the local
model** (free-form, not a curated string). **On CPU, pass a generous `timeout_seconds` (the chat
endpoint clamps it to ≤ 300 s — [`chat_handler.go`](../../internal/server/chat_handler.go))**: the
chat default is 30 s, and CPU-only inference runs well past it (you would otherwise get
`DEADLINE_EXCEEDED` — "agent did not respond in time"). CPU latency is highly variable — a small
model that ignores a length instruction and generates to its `max_tokens` cap can exceed even
120 s, so **constrain the output** (the prompt above asks for one short sentence) and use a high
`timeout_seconds`. The first turn also pays a one-time model load (~8 s); a warm turn is much
faster. On a GPU the default 30 s is usually enough. Reply *quality* is not asserted — a small
local model may emit a weak or malformed answer; this step checks routing, real tokens, and
`reply_status="ok"`.

**Verification**:
- [ ] `reply_status="ok"` with a non-empty, model-generated reply

---

### Step 4: Confirm Real Token Counts and $0 Cloud Spend

**Action**: Inspect the latest `agent.llm.call` span for `ember-owl` (Jaeger), and re-query the
cost summary.

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: The span reports **non-zero** `gen_ai.usage.input_tokens` /
`output_tokens` (a real model counted real tokens), and `gen_ai.system` reflects the
OpenAI-compatible Ollama provider. `daily_estimated_usd` stays at **$0** — the local model is
priced $0-real (no per-token cloud charge).

**Verification**:
- [ ] `gen_ai.usage.*_tokens` are non-zero (real tokens)
- [ ] `daily_estimated_usd` unchanged at $0 (no cloud spend)

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Local-model society up; model pulled; `OllamaProvider` active | ☐ |
| 2 | Cost baseline captured | ☐ |
| 3 | Chat round-trip returns a model-generated reply, `reply_status="ok"` | ☐ |
| 4 | Real non-zero token counts; $0 cloud spend | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: First Run — Model Pull In Progress

**Scenario**: On the first `make demo-ollama`, the multi-GB model pull is still running.

**Expected Behavior**: The agents gate on `ollama-pull` completing
(`service_completed_successfully`), so the first chat turn never races a missing model — it
waits for the pull rather than failing.

### Edge Case 2: `PERSATRIX_OLLAMA_MODEL` Override

**Scenario**: `PERSATRIX_OLLAMA_MODEL` is set to a non-default tag.

**Expected Behavior**: `create_provider()` substitutes that tag for every agent it builds, in
lock-step with the `ollama-pull` service (which pulls the same tag). **Caveat**: the override does
**not** reach the summarisation-on-close model, which resolves the `summarizer` alias on a separate
surface — match the three `model:` fields in the mounted config too, or summarisation-on-close
degrades to its fallback
([ISSUE-0075](../issues/ISSUE-0075-ollama-model-override-misses-summarization-surface.md)).
Covered by `test_llm_ollama.py`.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker Desktop | ✅ Pass | See [`v0.3.4-execution-report.md`](v0.3.4-execution-report.md#mt-ollama-001--ollama-local-model-evidence-live). `system=ollama`/`llama3.2`, real tokens (1232/4096) at $0 cloud, `reply_status="ok"` in ~93 s with `timeout_seconds:120`. (The default 30 s chat timeout is too short for CPU inference — [ISSUE-0073](../issues/ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md).) |
| 2026-05-27 | Claude (Opus 4.7) | Windows 11 + Docker 29.3.1 | ✅ Pass | **Config-driven re-run on HEAD `6ce23cd`** (`make demo-ollama`): `ollama list` shows `llama3.2:latest` (2.0 GB); the `ollama` branch logs **no** startup line (confirmed); real tokens (cumulative `3479/8192`) at **$0** cloud; Prometheus `agent_llm_calls_total{gen_ai_system="ollama", gen_ai_request_model="llama3.2"}=1`. A verbose turn at `timeout_seconds:120` hit `DEADLINE_EXCEEDED` (the model generated to its 4096-token cap); a constrained warm turn at `timeout_seconds:280` returned `reply_status="ok"` in ~10 s. CPU-latency caveat tracked as [ISSUE-0073](../issues/ISSUE-0073-cpu-local-model-chat-exceeds-default-timeout.md). |

---

## Notes

- The `ollama-models` volume persists across `docker compose down` so the multi-GB pull happens
  once. To reclaim the space, pass the overlay explicitly to `down -v`.
- The agents reach the daemon over the compose bridge at `http://ollama:11434/v1`, not
  `localhost` — `PERSATRIX_OLLAMA_BASE_URL` is set per agent in the overlay.
- Default model is `llama3.2` (the tag the mounted `config/demo/ollama/optimization.yaml` names
  and `ollama-pull` pulls); set `PERSATRIX_OLLAMA_MODEL` to change it — keep the pull and the alias
  `model:` fields in step (see Edge Case 2).
