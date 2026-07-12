# Manual Test MT-PROVIDER-WATSONX-001: IBM watsonx.ai — Cloud Provider Smoke

**Test ID**: `MT-PROVIDER-WATSONX-001`
**Feature Area**: Provider / watsonx.ai (IBM, native `ibm-watsonx-ai`)
**Version**: 1.0
**Created**: 2026-07-12
**Last Updated**: 2026-07-12
**Status**: Active

> **RFC 0053 — config-driven (no force-knob).** Provider selection is purely
> config/alias-driven: `make demo-watsonx` selects the watsonx provider by mounting
> [`config/demo/watsonx/optimization.yaml`](../../config/demo/watsonx/optimization.yaml)
> (every alias → `provider: watsonx`, priced) over the stack's `optimization.yaml`
> — the same one-line provider swap as `demo-openai`. watsonx is a **first-class**
> provider on the native `ibm-watsonx-ai` SDK (RFC 0053 §C — watsonx has no broad
> OpenAI-compatible endpoint, so a native class is required), so this is a
> **live, keyed, billed** smoke — the cloud sibling of the keyless
> [MT-OLLAMA-001](MT-OLLAMA-001.md).

---

## Overview

**Purpose**: Verify the native watsonx path end to end — *the whole society runs on
IBM's watsonx-hosted foundation models* (Llama / Granite), a chat turn completes
with **real** token counts, cost attributes to the priced watsonx aliases, and the
telemetry files the traffic under `gen_ai.system = watsonx` (not `openai`).

**Scope**: The `provider: watsonx` path through
[`agents/llm_factory.py`](../../agents/llm_factory.py) `create_provider()` — the
resolved alias's `provider` field selects the concrete class (RFC 0033 §D), the
**secret** key comes from `WATSONX_API_KEY` (env) while the **required non-secret**
`project_id` (or `space_id`) + regional `url` come from the alias `provider_config`
→ [`agents/llm_watsonx.py`](../../agents/llm_watsonx.py) `WatsonxProvider` (native
`ibm-watsonx-ai`, a per-model `ModelInference` built lazily, OpenAI-shaped chat
request/response, `tools` mapping, sync `chat` offloaded via `asyncio.to_thread`).
The `make demo-watsonx` overlay installs the optional `ibm-watsonx-ai` extra into
the agent image via the `AGENT_EXTRAS` build arg and plumbs the secret key only
(the base compose plumbs only Anthropic/OpenAI). Carried from RFC 0053 PR 2.

**Out of Scope**: Gemini (RFC 0053 PR 1, [MT-PROVIDER-GEMINI-001](MT-PROVIDER-GEMINI-001.md));
the four-vendor cross-provider brainstorm (`MT-AUTONOMOUS-MULTIPROVIDER-001`, RFC
0052 PR 9); `space_id` deployment routing (documented alternative to `project_id`,
not exercised here); tool-use on tool-capable watsonx models (the brainstorm demo
is conversation, not tool use); reply *quality*.

---

## Related Documentation

**Feature Documentation**:
- [docker-compose.watsonx.yaml](../../docker-compose.watsonx.yaml) — the overlay
  (mounts `config/demo/watsonx/optimization.yaml`; sets `AGENT_EXTRAS: watsonx`;
  plumbs the secret `WATSONX_API_KEY` only — `project_id`/`url` live in the config).
- [config/demo/watsonx/optimization.yaml](../../config/demo/watsonx/optimization.yaml)
  — the watsonx alias config (`quality` → `meta-llama/llama-3-3-70b-instruct`,
  `fast`/`summarizer` → `ibm/granite-3-8b-instruct`, priced; `project_id`/`url` in
  `provider_config`).
- [Makefile](../../Makefile) `demo-watsonx` target.
- [agents/llm_watsonx.py](../../agents/llm_watsonx.py) — `WatsonxProvider`.
- [agents/llm_factory.py](../../agents/llm_factory.py) — the `provider: watsonx`
  branch (required `project_id`/`url` fail-closed).

**Related Automated Tests**:
- Python: `tests/unit/python/test_llm_watsonx.py` (WatsonxProvider tool-round
  mapping, response normalisation) + `test_llm_factory_watsonx.py::TestCreateProviderWatsonx`
  (factory routing, **required `project_id`/`url` fail-closed**, missing-key warning,
  missing-SDK SystemExit).
- Go: `internal/security/redactor_ibm_test.go` (`watsonx-api-key` — `WATSONX_API_KEY=…`
  scrubbed from logs).

**Related Manual Tests**:
- [MT-PROVIDER-GEMINI-001](MT-PROVIDER-GEMINI-001.md) — the sibling cloud-provider smoke.
- [MT-OLLAMA-001](MT-OLLAMA-001.md) — the keyless local-provider sibling.

---

## Preconditions

### System Requirements

- ☐ Windows / macOS / Linux with Docker + Docker Compose.
- ☐ **A watsonx.ai API key** in your environment or `.env` as `WATSONX_API_KEY`
  (an IBM Cloud IAM key). **This is a real billed provider** — set a spending cap
  in IBM Cloud first.
- ☐ **A watsonx project id and regional URL** filled into
  `config/demo/watsonx/optimization.yaml` `provider_config` (they ship empty — the
  factory **fails closed at startup** until you set `project_id`; the `url` defaults
  to us-south). These are **config, not secrets** — they do **not** go in env.
- ☐ `curl` + `jq` in PATH.

### Application State

- ☐ `make demo-watsonx` builds the agent image **with the `ibm-watsonx-ai` extra**
  (`AGENT_EXTRAS: watsonx`, so the `--build` the target passes is required, not
  optional) and starts the society routed to watsonx.

### Test Data

None — the cloud model generates replies.

---

## Test Procedure

### Step 1: Fill In project_id, Then Bring Up the watsonx Society

**Action**:

```bash
# Edit config/demo/watsonx/optimization.yaml → set provider_config.project_id
# (and url for your region) on all three aliases. These are config, not secrets.
export WATSONX_API_KEY=...   # the secret IAM key
make demo-watsonx
docker compose logs agent-ember-owl | grep -iE "watsonx|ibm-watsonx|API_KEY|project_id" | tail
```

**Expected Result**: The stack builds and starts. Agents come up with **no**
`WATSONX_API_KEY … not set` warning (the key is plumbed) and **no** watsonx
`requires … project_id`/`url` `SystemExit` (you filled the config) and **no**
`ibm-watsonx-ai` `ImportError` (the extra installed at build). No raw-ID
deprecation warning (aliases route to `provider: watsonx`).

**Verification**:
- [ ] Society up; no missing-key warning; no fail-closed config `SystemExit`; no
  missing-SDK `SystemExit`.

---

### Step 2: Capture a Cost Baseline

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/watsonx-cost-before.json | python3 -m json.tool
```

**Verification**:
- [ ] Baseline captured (expected `0/0/$0` on a fresh stack).

---

### Step 3: Drive a Chat Round-Trip Through watsonx

**Action**:

```bash
curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/ember-owl/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"Reply with exactly one short sentence: what is your sprint priority?","user_id":"local","timeout_seconds":120}' \
  | jq '{reply_status, reply}'
```

**Expected Result**: HTTP 200, `reply_status="ok"`, a non-empty, model-generated
reply. Reply *quality* is not asserted — this checks routing, real tokens, and
`reply_status="ok"`.

**Verification**:
- [ ] `reply_status="ok"` with a non-empty reply.

---

### Step 4: Confirm Real Tokens, Real Cost, and `gen_ai.system=watsonx`

**Action**: Inspect the latest `agent.llm.call` span for `ember-owl` (Jaeger), and
re-query the cost summary.

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: The span reports **non-zero** `gen_ai.usage.input_tokens` /
`output_tokens`, `gen_ai.system = watsonx`, and `gen_ai.request.model` a physical
watsonx id (e.g. `meta-llama/llama-3-3-70b-instruct`, never an alias name).
`daily_estimated_usd` **increased** from the baseline — watsonx is a real per-token
cloud provider, and the priced aliases keep the RFC 0023 budget/lease gate live
(unlike the $0 offline / Ollama demos).

**Verification**:
- [ ] `gen_ai.system=watsonx`; `gen_ai.request.model` is a physical watsonx id.
- [ ] `gen_ai.usage.*_tokens` non-zero; `daily_estimated_usd` increased.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | watsonx society up; SDK extra present; key plumbed; project_id/url set | ☐ |
| 2 | Cost baseline captured | ☐ |
| 3 | Chat round-trip returns a model-generated reply, `reply_status="ok"` | ☐ |
| 4 | Real non-zero tokens; cost increased; `gen_ai.system=watsonx` | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Missing project_id / url (Fail Closed)

**Scenario**: `provider_config.project_id` (and `space_id`) or `url` is absent —
e.g. the shipped config's empty `project_id` left unedited.

**Expected Behavior**: `create_provider()` **fails closed** with a loud, actionable
`SystemExit` naming the missing field — deliberately louder than the missing-*key*
warning, because required config the client cannot construct without should fail at
startup, not defer to the first request. The society does not silently run.

### Edge Case 2: Missing Key

**Scenario**: `WATSONX_API_KEY` unset (but `project_id`/`url` present).

**Expected Behavior**: The factory **warns** at startup (S-09) and returns a
provider — the per-model client is built lazily, so the failure surfaces on the
**first request** (an auth error), not at construction. The society does not crash
on boot. (This is the softer posture reserved for the *secret*, which is recoverable
per-request — unlike the required config above.)

### Edge Case 3: Missing SDK

**Scenario**: The `ibm-watsonx-ai` extra is not installed (e.g. running the base
image without `AGENT_EXTRAS: watsonx`).

**Expected Behavior**: `create_provider()` raises a loud, actionable `SystemExit`
naming the install (`pip install 'ibm-watsonx-ai>=1.1.0'`, or the extra
`pip install 'persatrix-agents[watsonx]'`), not a raw `ImportError` traceback.

### Edge Case 4: Unpriced Alias

**Scenario**: A `watsonx` alias without `input_per_1m_tokens` / `output_per_1m_tokens`.

**Expected Behavior**: The RFC 0033 §F missing-price guard fails closed — a
non-local provider with no price is rejected rather than silently reading $0 and
disabling the budget gate. Every shipped demo alias is priced.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| _pending_ | — | — | — | Live run scheduled for v0.3.11 release-prep (master-plan Phase 3). |

---

## Notes

- The **secret** key rides compose env; the **non-secret** `project_id`/`url` belong
  in the alias `provider_config`, not env — the same split OpenAI's `base_url` uses,
  and the single source of truth the factory validates (fail-closed on absence).
- The `WATSONX_API_KEY=…` assignment is scrubbed from logs by the `watsonx-api-key`
  redactor pattern, pinned by `internal/security/redactor_ibm_test.go`. (IBM Cloud
  IAM keys have no distinctive standalone prefix, so — unlike Google's `AIza…` — the
  redactor pins the assignment surface rather than shape-matching a bare key.)
