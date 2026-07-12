# Manual Test MT-PROVIDER-GEMINI-001: Google Gemini — Cloud Provider Smoke

**Test ID**: `MT-PROVIDER-GEMINI-001`
**Feature Area**: Provider / Gemini (Google, native `google-genai`)
**Version**: 1.0
**Created**: 2026-07-11
**Last Updated**: 2026-07-12
**Status**: Active

> **RFC 0053 — config-driven (no force-knob).** Provider selection is purely
> config/alias-driven: `make demo-gemini` selects the Gemini provider by mounting
> [`config/demo/gemini/optimization.yaml`](../../config/demo/gemini/optimization.yaml)
> (every alias → `provider: gemini`, priced) over the stack's `optimization.yaml`
> — the same one-line provider swap as `demo-openai`. Gemini is a **first-class**
> provider on the native `google-genai` SDK (RFC 0053 OQ #1 — a clean `gemini`
> identity for cost/telemetry, not the OpenAI-compat endpoint), so this is a
> **live, keyed, billed** smoke — the cloud sibling of the keyless
> [MT-OLLAMA-001](MT-OLLAMA-001.md).

---

## Overview

**Purpose**: Verify the native Gemini path end to end — *the whole society runs on
Google's Gemini models*, a chat turn completes with **real** token counts, cost
attributes to the priced `gemini-3.5-flash` aliases, and the telemetry files the traffic
under `gen_ai.system = gemini` (not `openai`).

**Scope**: The `provider: gemini` path through
[`agents/llm_factory.py`](../../agents/llm_factory.py) `create_provider()` — the
resolved alias's `provider` field selects the concrete class (RFC 0033 §D), the key
comes from `GEMINI_API_KEY` (fallback `GOOGLE_API_KEY`) →
[`agents/llm_gemini.py`](../../agents/llm_gemini.py) `GeminiProvider` (native
`google-genai`, `contents`/`config` request build, `function_declarations` tool
mapping, `candidates` response normalisation). The `make demo-gemini` overlay
installs the optional `google-genai` extra into the agent image via the
`AGENT_EXTRAS` build arg and plumbs the key (the base compose plumbs only
Anthropic/OpenAI). Carried from RFC 0053 PR 1.

**Out of Scope**: watsonx.ai (RFC 0053 PR 2); the four-vendor cross-provider
brainstorm (`MT-AUTONOMOUS-MULTIPROVIDER-001`, RFC 0052 PR 9); Vertex AI routing
(`provider_config.project`/`location` — documented, not exercised here); reply
*quality*.

---

## Related Documentation

**Feature Documentation**:
- [docker-compose.gemini.yaml](../../docker-compose.gemini.yaml) — the overlay
  (mounts `config/demo/gemini/optimization.yaml`; sets `AGENT_EXTRAS: gemini`;
  plumbs `GEMINI_API_KEY` / `GOOGLE_API_KEY`).
- [config/demo/gemini/optimization.yaml](../../config/demo/gemini/optimization.yaml)
  — the Gemini alias config (`quality` / `fast` / `summarizer` → `gemini-3.5-flash`,
  priced).
- [Makefile](../../Makefile) `demo-gemini` target.
- [agents/llm_gemini.py](../../agents/llm_gemini.py) — `GeminiProvider`.
- [agents/llm_factory.py](../../agents/llm_factory.py) — the `provider: gemini` branch.

**Related Automated Tests**:
- Python: `tests/unit/python/test_llm_gemini.py` (GeminiProvider tool-round mapping,
  response normalisation, factory routing / key fallback / missing-key warning /
  missing-SDK SystemExit).
- Go: `internal/security/redactor_google_test.go` (`google-api-key` — `AIza…` keys
  scrubbed from logs).

**Related Manual Tests**:
- [MT-OLLAMA-001](MT-OLLAMA-001.md) — the keyless local-provider sibling.

---

## Preconditions

### System Requirements

- ☐ Windows / macOS / Linux with Docker + Docker Compose.
- ☐ **A Gemini API key** in your environment or `.env` as `GEMINI_API_KEY` (or
  `GOOGLE_API_KEY`). Get one from Google AI Studio. **This is a real billed
  provider** — set a spending cap first.
- ☐ `curl` + `jq` in PATH.

### Application State

- ☐ `make demo-gemini` builds the agent image **with the `google-genai` extra**
  (`AGENT_EXTRAS: gemini`, so the `--build` the target passes is required, not
  optional) and starts the society routed to Gemini.

### Test Data

None — the cloud model generates replies.

---

## Test Procedure

### Step 1: Bring Up the Gemini Society

**Action**:

```bash
export GEMINI_API_KEY=...   # or GOOGLE_API_KEY
make demo-gemini
docker compose logs agent-ember-owl | grep -iE "gemini|google-genai|API_KEY" | tail
```

**Expected Result**: The stack builds and starts. Agents come up with **no**
`GEMINI_API_KEY … not set` warning (the key is plumbed) and **no**
`google-genai` `ImportError`/`SystemExit` (the extra installed at build). No
raw-ID deprecation warning (aliases route to `provider: gemini`).

**Verification**:
- [ ] Society up; no missing-key warning; no missing-SDK `SystemExit`.

---

### Step 2: Capture a Cost Baseline

**Action**:

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | tee /tmp/gemini-cost-before.json | python3 -m json.tool
```

**Verification**:
- [ ] Baseline captured (expected `0/0/$0` on a fresh stack).

---

### Step 3: Drive a Chat Round-Trip Through Gemini

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

### Step 4: Confirm Real Tokens, Real Cost, and `gen_ai.system=gemini`

**Action**: Inspect the latest `agent.llm.call` span for `ember-owl` (Jaeger), and
re-query the cost summary.

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: The span reports **non-zero** `gen_ai.usage.input_tokens` /
`output_tokens`, `gen_ai.system = gemini`, and `gen_ai.request.model` a physical
`gemini-3.5-flash` id (never an alias name). `daily_estimated_usd` **increased** from
the baseline — Gemini is a real per-token cloud provider, and the priced aliases
keep the RFC 0023 budget/lease gate live (unlike the $0 offline / Ollama demos).

**Verification**:
- [ ] `gen_ai.system=gemini`; `gen_ai.request.model` is a physical `gemini-3.5-flash` id.
- [ ] `gen_ai.usage.*_tokens` non-zero; `daily_estimated_usd` increased.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Gemini society up; SDK extra present; key plumbed | ☐ |
| 2 | Cost baseline captured | ☐ |
| 3 | Chat round-trip returns a model-generated reply, `reply_status="ok"` | ☐ |
| 4 | Real non-zero tokens; cost increased; `gen_ai.system=gemini` | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Missing Key

**Scenario**: `GEMINI_API_KEY` / `GOOGLE_API_KEY` unset.

**Expected Behavior**: The factory **warns** at startup (S-09) and returns a
provider — the client is built lazily, so the failure surfaces on the **first
request** (an auth error), not at construction. The society does not crash on boot.

### Edge Case 2: Missing SDK

**Scenario**: The `google-genai` extra is not installed (e.g. running the base
image without `AGENT_EXTRAS: gemini`).

**Expected Behavior**: `create_provider()` raises a loud, actionable `SystemExit`
naming the install (`pip install 'google-genai>=1.0.0'`, or the extra
`pip install 'persatrix-agents[gemini]'`), not a raw `ImportError` traceback.

### Edge Case 3: Unpriced Alias

**Scenario**: A `gemini` alias without `input_per_1m_tokens` / `output_per_1m_tokens`.

**Expected Behavior**: The RFC 0033 §F missing-price guard fails closed — a
non-local provider with no price is rejected rather than silently reading $0 and
disabling the budget gate. Every shipped demo alias is priced.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-07-12 | Claude Code (automated) | macOS / Docker | **PASS** | Steps 1–4 driven via curl. First run failed: `gemini-2.5-pro` / `gemini-2.5-flash` returned `404 … no longer available to new users` → chat `DEADLINE_EXCEEDED`. Repointed the demo aliases to `gemini-3.5-flash` (config); re-ran: `reply_status="ok"`, `gen_ai.system=gemini`, `gen_ai.request.model=gemini-3.5-flash`, real tokens, cost accrued. |

---

## Notes

- The secret key rides compose env; any **non-secret** Vertex knobs
  (`project`/`location`) belong in the alias `provider_config`, not env — the same
  split OpenAI's `base_url` uses.
- Keys are scrubbed from logs by the `google-api-key` redactor pattern (`AIza…`),
  pinned by `internal/security/redactor_google_test.go`.
- **Model lifecycle (2026-07-12):** Google retired `gemini-2.5-pro` / `gemini-2.5-flash`
  for new API users (`generateContent` → `404 … no longer available to new users`,
  even though `models.list()` still advertises them). The demo config was repointed
  to `gemini-3.5-flash` for all three aliases. If this smoke ever regresses to
  `DEADLINE_EXCEEDED` with a 404 in the agent logs, re-check the alias models against
  Google's currently-available list and repoint `config/demo/gemini/optimization.yaml`.
