# Manual Test MT-COST-003: Chat Budget Exceed Surfaces as `reply_status="error"`

**Test ID**: `MT-COST-003`
**Feature Area**: Cost
**Version**: 1.0
**Created**: 2026-05-20
**Last Updated**: 2026-05-20
**Status**: Active

---

## Overview

**Purpose**: Verify that when a chat session exhausts its budget, the orchestrator
`POST /api/v1/agents/{id}/chat` endpoint surfaces the wallet denial as a structured
`reply_status="error"` carrying the wallet's `LeaseDenied.message` — not a 500-level error, not
a silent empty reply, and not a generic `"Internal error"` reply. Exercises the RFC 0023 PR 4
chat-path lease wiring and the closed v0.2.3 chat-bypass.

**Scope**: `WalletService.AcquireLease` denial on the chat path, `BudgetExceededError`
propagation through `EventDispatcher.dispatch` → `AgentServiceServicer.SendChatMessage`,
`ChatResponse.reply_status` / `ChatResponse.reply` semantics on denial, HTTP-200 envelope
with the structured-error body.

**Out of Scope**: Per-cause budget policies (RFC 0023 Phase 7+); chat traffic on the
*receiver* channel path (covered by MT-CHANNEL-*); workflow budget enforcement (MT-COST-002).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0023-llm-call-leasing.md](../rfcs/0023-llm-call-leasing.md) — §E (Python client
  integration), §F (failure modes).
- [docs/rfcs/0023-pr-plan.md](../rfcs/0023-pr-plan.md) — PR 4 (this test).
- [agents/server_servicers.py](../../agents/server_servicers.py) — `AgentServiceServicer.SendChatMessage`.
- [agents/persona_runtime/wallet_cause.py](../../agents/persona_runtime/wallet_cause.py) — chat
  cause derivation (`cause_for_event`).
- [agents/persona_runtime/action_loop.py](../../agents/persona_runtime/action_loop.py) — call
  site that passes `cause=cause_for_event(event)` into `LLMClient.create_message`.
- [agents/wallet_client.py](../../agents/wallet_client.py) — `BudgetExceededError`.

**Related Automated Tests**:
- Unit: `agents/tests/test_action_loop_chat_lease.py`
- Unit: `agents/tests/test_chat_path_budget_denial.py`
- Integration: `tests/integration/test_chat_budget_exhaustion.py`

**Related Manual Tests**:
- [MT-COST-002](MT-COST-002.md) — workflow-step budget abort (the post-hoc RFC 0006 path this
  RFC 0023 lease shape now front-runs for the chat origin).
- [MT-CHAT-001](MT-CHAT-001.md) — baseline chat round-trip (the happy path this test inverts).

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Go 1.24+: `go version`
- Python 3.11+: `python3 --version`
- `curl` available in PATH
- `jq` available in PATH (response inspection)
- `ANTHROPIC_API_KEY` set

### Application State

- ☐ Orchestrator running: `make run`
- ☐ One persona agent registered and connected
- ☐ Config valid: `make validate` exits 0

### Test Data

The orchestrator's wallet must enforce a `per_agent` budget low enough to deny on the second
or third chat turn. Edit `config/optimization.yaml` so the persona agent's `per_agent` budget
is small (e.g. `0.02` USD), then restart the orchestrator. Note the pre-edit value so Step 5
can restore it. The schema below matches the live keys (`cost.budgets.*` for budgets, top-level
`wallet:` for lease tuning); see `schemas/optimization.schema.json` for the authoritative shape.

```yaml
cost:
  budgets:
    global:
      max_daily_usd: 100.00
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 0.02      # tight cap — denies after ~1 chat turn

# Top-level — sibling of `cost:`. RFC 0023 wallet lease lifecycle tuning.
wallet:
  ttl_seconds: 60
  reaper_interval_seconds: 5
  max_active_leases: 16
```

Run `make validate` after the edit and confirm the cap is in effect via
`GET /api/v1/cost/summary`.

---

## Test Procedure

### Step 1: Establish a Baseline Chat Turn (Within Budget)

**Action**:

```bash
AGENT_ID=<the registered persona agent id>
RESP=$(curl -s -w "\nHTTP %{http_code}\n" \
  -X POST "http://127.0.0.1:8080/api/v1/agents/${AGENT_ID}/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"hello, say a short greeting back","user_id":"local"}')
echo "$RESP"
SESSION_ID=$(echo "$RESP" | head -n -1 | jq -r .chat_session_id)
echo "session=$SESSION_ID"
```

**Expected Result**: HTTP 200 with `reply_status="ok"` and a non-empty `reply`. Note the
`chat_session_id` for Step 2.

**Verification**:
- [ ] HTTP status 200
- [ ] `reply_status` field equals `"ok"`
- [ ] `reply` field is non-empty
- [ ] `chat_session_id` is non-empty

---

### Step 2: Exhaust the Budget Mid-Conversation

**Action**: Re-issue chat turns using the same `chat_session_id` until the wallet refuses.
With `per_agent_usd: 0.02` a single non-trivial reply typically exhausts the cap on the
second turn.

```bash
for i in 1 2 3 4 5; do
  RESP=$(curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/${AGENT_ID}/chat" \
    -H "Content-Type: application/json" \
    -d "{\"message\":\"turn ${i} — please answer in full\",
         \"user_id\":\"local\",
         \"chat_session_id\":\"${SESSION_ID}\"}")
  echo "--- turn $i ---"
  echo "$RESP" | jq '{reply_status, reply}'
  STATUS=$(echo "$RESP" | jq -r .reply_status)
  if [ "$STATUS" = "error" ]; then
    echo "(budget denied on turn $i)"
    break
  fi
done
```

**Expected Result**: One of the turns transitions from `reply_status="ok"` to
`reply_status="error"`. The HTTP status of the denied turn is **200** (the chat *call*
succeeded — only the wallet refused to fund the LLM call inside), and `reply` carries the
wallet's structured `LeaseDenied.message` — typically containing the substring
`"budget exceeded"` and the offending scope (e.g. `per_agent`).

**Verification**:
- [ ] HTTP status of the denied turn is 200 (not 500, not 503)
- [ ] `reply_status` on the denied turn equals `"error"`
- [ ] `reply` on the denied turn is non-empty
- [ ] `reply` text references `budget` or `lease` (not a generic `"Internal error"`)

---

### Step 3: Verify Subsequent Turns Stay Denied Until the Budget Resets

**Action**: Continue issuing chat turns in the same session without resetting the budget.

```bash
for i in 6 7; do
  RESP=$(curl -s -X POST "http://127.0.0.1:8080/api/v1/agents/${AGENT_ID}/chat" \
    -H "Content-Type: application/json" \
    -d "{\"message\":\"another turn ${i}\",\"user_id\":\"local\",
         \"chat_session_id\":\"${SESSION_ID}\"}")
  echo "--- post-deny turn $i ---"
  echo "$RESP" | jq '{reply_status, reply}'
done
```

**Expected Result**: Both turns return `reply_status="error"` with the same denial message
shape. The wallet does not silently recover; subsequent traffic must continue to be denied
until the budget window resets.

**Verification**:
- [ ] Every post-denial turn has `reply_status="error"`
- [ ] No turn after the first denial silently succeeds

---

### Step 4: Verify No Provider Spend Was Charged for Denied Turns

**Action**: Inspect the orchestrator's cost summary; the denied turns must not have advanced
either the input- or output-token counters.

```bash
curl -s http://127.0.0.1:8080/api/v1/cost/summary | python3 -m json.tool
```

**Expected Result**: Token counts reflect *only* the successful turns; denied turns left no
provider-spend trace (the denial happens *before* the provider is contacted, RFC 0023 § F).
The wallet's provisional charge is reversed on the denied-lease exit.

**Verification**:
- [ ] `daily_output_tokens` advanced only by the successful turns from Steps 1–2
- [ ] No token spike correlated to the denied turns

---

### Step 5: Restore the Original Budget Config

**Action**: Revert the `config/optimization.yaml` edit from Preconditions, restart the
orchestrator, and confirm `GET /api/v1/cost/summary` reflects the restored cap.

**Verification**:
- [ ] `config/optimization.yaml` matches the pre-test value
- [ ] `make validate` exits 0
- [ ] Orchestrator restart cleanly registers the persona agent

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Baseline chat turn returns `reply_status="ok"` with a non-empty reply | ☐ |
| 2 | A subsequent turn transitions to `reply_status="error"`; HTTP 200; reply carries the denial message | ☐ |
| 3 | Post-denial turns continue to return `reply_status="error"` | ☐ |
| 4 | Cost summary shows no spend trace for the denied turns | ☐ |
| 5 | Original budget config restored | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Wallet Unreachable

**Scenario**: The orchestrator's gRPC listener (`--orchestrator-grpc`) is down when the chat
turn arrives. The agent's `WalletClient` raises `BudgetExceededError(reason="wallet_unreachable")`.

**Expected Behavior**: Chat path surfaces the same `reply_status="error"` shape, with `reply`
text indicating the wallet was unreachable (RFC 0023 § F — fail closed). The chat call still
returns HTTP 200; the wallet outage is *not* surfaced as a 5xx because the chat *RPC* itself
succeeded.

### Edge Case 2: Wallet Permits the Turn but the Provider Fails

**Scenario**: Wallet grants the lease, but the LLM provider returns a 5xx / connection error.

**Expected Behavior**: The persona action loop's generic `except Exception` arm catches the
provider error and surfaces it as a `COMPLETE_TASK` action with `result="LLM provider error"`,
which the chat-reply extractor maps to `reply_status="ok"` with `reply="LLM provider error"`.
*This is the pre-PR 4 behaviour and is unchanged* — PR 4 distinguishes only budget denials.
The wallet's provisional charge is reconciled at settle-at-granted on the lease's context
exit (RFC 0023 § F), so the provider outage does not silently leak free spend.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|

(Execution is deferred to the v0.3.2 release-prep Phase 4 PR 1 manual-test report per the
v0.3.2 master plan.)

---

## Notes

- This test exercises the chat-path lease wiring introduced in RFC 0023 PR 4. The v0.2.3
  chat-bypass — chat traffic skipping `BudgetEnforcer` entirely — closes here; the README
  known-limitation line is deleted in v0.3.2 release-prep (Phase 4 PR 2), not in this PR.
- The `per_agent_usd` value in Preconditions is intentionally small to make the denial
  reproducible within 1–3 turns; a real deployment would set this to operationally meaningful
  values.
- The denied-turn HTTP status is **200**, *not* 5xx, by design. The chat RPC succeeded — the
  LLM call inside it was denied. The gRPC status stays `OK`; the structured denial rides in
  the response body. Surfacing budget denials as 5xx would conflate them with chat-server
  failures and break dashboard incident routing.
