# Manual Test MT-CHAT-001: Chat REST Endpoint — Send Message, Receive Reply

**Test ID**: `MT-CHAT-001`
**Feature Area**: Chat
**Version**: 1.0
**Created**: 2026-04-20
**Last Updated**: 2026-04-20
**Status**: Active

---

## Overview

**Purpose**: Verify that `POST /api/v1/agents/{id}/chat` accepts a chat message, routes it through
the gRPC `SendChatMessage` RPC to a registered persona agent, and returns a well-formed JSON
response. Also verify error handling for missing agents, empty messages, and oversized messages.

**Scope**: REST endpoint request/response shape, HTTP status codes, message length validation
(4 000 UTF-8 character limit), unknown-agent 404, and basic round-trip with a live agent.

**Out of Scope**: CLI REPL (covered by MT-CHAT-002); session continuity across restarts (MT-CHAT-003);
relationship memory evolution (MT-CHAT-004).

---

## Related Documentation

**Feature Documentation**:
- [internal/server/chat_handler.go](../../internal/server/chat_handler.go) — REST handler
- [internal/server/types.go](../../internal/server/types.go) — request/response structs
- [internal/executor/chat.go](../../internal/executor/chat.go) — gRPC chat executor
- [agents/server_servicers.py](../../agents/server_servicers.py) — agent-side `SendChatMessage`
- [proto/task.proto](../../proto/task.proto) — `ChatRequest` / `ChatResponse` messages

**Related Automated Tests**:
- Unit tests: `tests/unit/python/test_agents.py`
- Integration tests: `tests/integration/test_agent_server.py`

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

### Application State

- ☐ Orchestrator running: `make run` (defaults to `http://localhost:8080`)
- ☐ At least one persona agent registered and healthy (e.g. `ember-owl`):
  `make run-agent` (defaults to `127.0.0.1:50051`)
- ☐ Config files valid: `make validate`
- ☐ `ANTHROPIC_API_KEY` set in environment (required for Steps 1–3 which hit the LLM)

### Test Data

No external fixtures required. All requests are constructed inline with `curl`.

---

## Test Procedure

### Step 1: Send a Simple Chat Message

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, how are you today?", "user_id": "test-user"}'
```

**Expected Result**: HTTP 200 with a JSON body containing all required fields.

Example response shape:

```json
{
  "reply": "...",
  "chat_session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "agent_id": "ember-owl",
  "timestamp": 1745107200,
  "agent_display_name": "ember-owl",
  "reply_status": "ok"
}
```

**Verification**:
- [ ] HTTP status is `200`
- [ ] `reply` is a non-empty string
- [ ] `chat_session_id` is a non-empty UUID-like string
- [ ] `agent_id` matches the requested agent (`ember-owl`)
- [ ] `timestamp` is a positive integer (Unix epoch seconds)
- [ ] `agent_display_name` is non-empty
- [ ] `reply_status` is `"ok"`

---

### Step 2: Reuse Session ID for Follow-up Message

**Action**: Copy the `chat_session_id` from Step 1 and include it in a follow-up request:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Can you remember what I just said?", "user_id": "test-user", "chat_session_id": "<CHAT_SESSION_ID_FROM_STEP_1>"}'
```

**Expected Result**: HTTP 200. The response `chat_session_id` matches the one sent.

**Verification**:
- [ ] HTTP status is `200`
- [ ] `chat_session_id` in response equals the value sent in the request
- [ ] `reply_status` is `"ok"`

---

### Step 3: Omit Optional Fields (Defaults Applied)

**Action**: Send a request with only the required `message` field:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Just the message, nothing else."}'
```

**Expected Result**: HTTP 200. Server assigns defaults: `user_id` defaults to `"local"`,
`chat_session_id` is server-generated, `participant_type` defaults to `"user"`.

**Verification**:
- [ ] HTTP status is `200`
- [ ] `chat_session_id` is present and non-empty (server-generated)
- [ ] `reply_status` is `"ok"` or `"empty"`

---

### Step 4: Unknown Agent Returns 404

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://localhost:8080/api/v1/agents/nonexistent-agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello?"}'
```

**Expected Result**: HTTP 404 with an error body.

**Verification**:
- [ ] HTTP status is `404`
- [ ] Response body contains an error message indicating the agent was not found

---

### Step 5: Empty Message Returns 400

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": ""}'
```

**Expected Result**: HTTP 400 indicating the message field is required / empty.

**Verification**:
- [ ] HTTP status is `400`
- [ ] Response body contains an error referencing the empty message

---

### Step 6: Oversized Message (> 4 000 Characters) Returns 400

**Action**: Generate a message exceeding 4 000 UTF-8 characters and send it:

```bash
# Generate a 4001-character message
MSG=$(python3 -c "print('A' * 4001)")
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"$MSG\"}"
```

**Expected Result**: HTTP 400 indicating the message exceeds the maximum length.

**Verification**:
- [ ] HTTP status is `400`
- [ ] Response body contains an error referencing message length or size
- [ ] A message of exactly 4 000 characters is accepted (boundary check — optional)

---

### Step 7: Invalid Agent ID Format Returns 400

**Action**:

```bash
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://localhost:8080/api/v1/agents/INVALID_AGENT!/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello?"}'
```

**Expected Result**: HTTP 400 indicating an invalid agent ID format.

**Verification**:
- [ ] HTTP status is `400`
- [ ] Response body references the invalid agent ID pattern

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | HTTP 200 with well-formed chat response | ☐ |
| 2 | HTTP 200 with matching chat_session_id | ☐ |
| 3 | HTTP 200 with server-assigned defaults | ☐ |
| 4 | HTTP 404 for unknown agent | ☐ |
| 5 | HTTP 400 for empty message | ☐ |
| 6 | HTTP 400 for oversized message (> 4 000 chars) | ☐ |
| 7 | HTTP 400 for invalid agent ID format | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Missing Content-Type Header

**Scenario**: Send a POST without `Content-Type: application/json`.

**Expected**: HTTP 400 or the server rejects the body parsing.

### Edge Case 2: Exactly 4 000 Characters (Boundary)

**Scenario**: Send a message of exactly 4 000 UTF-8 characters.

**Expected**: HTTP 200 — request is accepted.

### Edge Case 3: Multi-byte UTF-8 Characters

**Scenario**: Send a message with multi-byte characters (e.g. emoji, CJK) that is under 4 000
*characters* but over 4 000 *bytes*.

**Expected**: HTTP 200 — the limit is on characters (Unicode code points), not bytes.

### Edge Case 4: Missing JSON Body

**Scenario**: `curl -X POST .../chat` with no `-d` flag.

**Expected**: HTTP 400 — the server rejects a missing or empty body.

---

## API Key Requirement

| Step | Requires `ANTHROPIC_API_KEY` |
|------|------------------------------|
| 1 | Yes (agent calls LLM) |
| 2 | Yes |
| 3 | Yes |
| 4 | No (404 before agent call) |
| 5 | No (400 before agent call) |
| 6 | No (400 before agent call) |
| 7 | No (400 before agent call) |
