# Manual Test MT-CHAT-002: `persatrix chat` CLI Interactive Session

**Test ID**: `MT-CHAT-002`
**Feature Area**: Chat
**Version**: 1.0
**Created**: 2026-04-20
**Last Updated**: 2026-04-20
**Status**: Active

---

## Overview

**Purpose**: Verify that `persatrix chat <agent_id>` opens an interactive REPL session, sends
messages via the REST chat endpoint, displays agent replies, reuses the session ID across messages,
and exits cleanly via `exit` and Ctrl-C.

**Scope**: CLI REPL flow: prompt display, message send/receive, session reuse, spinner behaviour,
error resilience, and clean exit.

**Out of Scope**: REST endpoint validation (MT-CHAT-001); memory persistence across restarts
(MT-CHAT-003); relationship memory evolution (MT-CHAT-004).

---

## Related Documentation

**Feature Documentation**:
- [cli/src/commands/chat.rs](../../cli/src/commands/chat.rs) — chat REPL implementation
- [cli/src/main.rs](../../cli/src/main.rs) — `Chat` command definition
- [internal/server/chat_handler.go](../../internal/server/chat_handler.go) — REST handler

**Related Automated Tests**:
- None — interactive REPL tests require a live server and terminal.

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Rust CLI built: `make build-cli` (binary at `cli/target/release/persatrix`)
- Go 1.24+: `go version`
- Python 3.11+: `python3 --version`

### Application State

- ☐ Orchestrator running: `make run` (defaults to `http://localhost:8080`)
- ☐ At least one persona agent registered and healthy (e.g. `ember-owl`):
  `make run-agent`
- ☐ Config files valid: `make validate`
- ☐ `ANTHROPIC_API_KEY` set in environment (required for agent to produce LLM replies)

### Test Data

No external fixtures required. All interaction is through the terminal.

---

## Test Procedure

### Step 1: Start a Chat Session

**Action**: Open a terminal and run:

```bash
./cli/target/release/persatrix chat ember-owl
```

**Expected Result**: The CLI prints a connection banner and presents the `You: ` prompt:

```
Connected to ember-owl. Type 'exit' or Ctrl-C to quit.
You: 
```

**Verification**:
- [ ] Banner line includes the agent ID (`ember-owl`)
- [ ] `You: ` prompt is displayed (bold text)
- [ ] No error messages printed

---

### Step 2: Send Messages and Receive Replies

**Action**: Type a message and press Enter:

```
You: Hello! What can you tell me about yourself?
```

Wait for the agent's reply to appear.

**Expected Result**: After a brief pause (spinner may appear after ~2 seconds), the agent's reply
is displayed with the display name in colour:

```
ember-owl: <agent's reply text>
You: 
```

**Verification**:
- [ ] Agent display name appears before the reply text
- [ ] Reply text is non-empty
- [ ] A new `You: ` prompt appears after the reply

---

### Step 3: Send a Second Message (Session Reuse)

**Action**: Send a follow-up message referencing the first exchange:

```
You: Can you summarize what we just talked about?
```

**Expected Result**: The agent replies. Internally, the same `chat_session_id` from the first
response is sent in this request (verifiable via orchestrator logs if needed).

**Verification**:
- [ ] Reply is displayed with agent display name
- [ ] No error about session or connection
- [ ] (Optional) Orchestrator logs show the same `chat_session_id` for both requests

---

### Step 4: Empty Input is Ignored

**Action**: Press Enter without typing anything (empty line).

**Expected Result**: The `You: ` prompt reappears immediately. No request is sent.

**Verification**:
- [ ] `You: ` prompt reappears without delay
- [ ] No HTTP request visible in orchestrator logs for the empty input

---

### Step 5: Exit via `exit` Command

**Action**: Type `exit` and press Enter:

```
You: exit
```

**Expected Result**: The REPL exits cleanly with no error output. The process terminates with
exit code 0.

**Verification**:
- [ ] REPL exits without printing an error
- [ ] Process exit code is `0`
- [ ] Shell prompt returns

---

### Step 6: Exit via Ctrl-C

**Action**: Start a new chat session:

```bash
./cli/target/release/persatrix chat ember-owl
```

After the `You: ` prompt appears, press **Ctrl-C**.

**Expected Result**: The REPL exits. On Windows PowerShell the exit code may be non-zero (this
is expected for SIGINT-style termination).

**Verification**:
- [ ] REPL exits without a crash or panic
- [ ] Shell prompt returns
- [ ] No stack trace or panic message in output

---

### Step 7: Custom User ID via `--user` Flag

**Action**:

```bash
./cli/target/release/persatrix chat ember-owl --user my-custom-user
```

Send one message:

```
You: Hello from a custom user.
```

**Expected Result**: HTTP 200 reply. The orchestrator log (or the agent's relationship memory)
records the interaction with `user_id = "my-custom-user"` rather than the default `"local"`.

**Verification**:
- [ ] Reply is displayed normally
- [ ] (Optional) Verify via orchestrator logs or relationship memory that `user_id` is
  `"my-custom-user"`

Type `exit` to end the session.

---

### Step 8: Chat with Non-existent Agent

**Action**:

```bash
./cli/target/release/persatrix chat nonexistent-agent
```

Send a message:

```
You: Hello?
```

**Expected Result**: The CLI prints an error (HTTP 404 from the server) but does not crash. The
REPL continues to show the `You: ` prompt, allowing the user to retry or exit.

**Verification**:
- [ ] An error message is displayed (referencing 404 or agent not found)
- [ ] The REPL does not crash — `You: ` prompt reappears
- [ ] Typing `exit` terminates normally

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Connection banner and `You: ` prompt displayed | ☐ |
| 2 | Agent reply displayed with display name | ☐ |
| 3 | Follow-up reply with session reuse | ☐ |
| 4 | Empty input ignored, prompt reappears | ☐ |
| 5 | `exit` command terminates cleanly (exit code 0) | ☐ |
| 6 | Ctrl-C terminates without crash | ☐ |
| 7 | `--user` flag sets custom user_id | ☐ |
| 8 | Non-existent agent shows error, REPL survives | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Orchestrator Not Running

**Scenario**: Run `persatrix chat ember-owl` with no orchestrator.

**Expected**: Connection error printed. REPL may exit or show error on first send.

### Edge Case 2: Agent Becomes Unavailable Mid-Session

**Scenario**: Stop the agent process while a chat session is active, then send a message.

**Expected**: Error printed (503 or connection refused). REPL survives; user can exit gracefully.

### Edge Case 3: Spinner Appears for Slow Responses

**Scenario**: Agent takes > 2 seconds to respond (e.g. complex LLM call).

**Expected**: Animated spinner (`⠋ ⠙ ...`) appears with "Waiting for ember-owl..." text, then
clears when the reply arrives.

---

## API Key Requirement

| Step | Requires `ANTHROPIC_API_KEY` |
|------|------------------------------|
| 1 | No (connection only) |
| 2 | Yes (agent calls LLM) |
| 3 | Yes |
| 4 | No (no request sent) |
| 5 | No (exit only) |
| 6 | No (exit only) |
| 7 | Yes (agent calls LLM) |
| 8 | No (404 before agent call) |
