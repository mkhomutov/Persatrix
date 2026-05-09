# Manual Test MT-CHANNEL-002: `persatrix channel send` / `reply` / `history` against a docker-composed orchestrator

**Test ID**: `MT-CHANNEL-002`
**Feature Area**: Channels (CLI)
**Version**: 1.0
**Created**: 2026-05-09
**Last Updated**: 2026-05-09
**Status**: Active

---

## Overview

**Purpose**: Verify the publish + read half of the Rust CLI channel surface (RFC 0011 §F)
end-to-end against a live orchestrator. Exercises the full set of `send` flag combinations
(`--mention`, `--mention-all`, `--as`, `--json`, `thread_id` via `reply`), the
`reply`-specific `validate_message_id` guard, and `history` (default page, `--limit`,
`--json`).

**Scope**: `channel send`, `channel reply`, `channel history` subcommands and the helpers
in [`channel.rs`](../../cli/src/commands/channel.rs): `expand_mentions`,
`validate_message_id`, `validate_send_inputs`, `validate_mention_count`,
`canonicalize_channel_id`.

**Out of Scope**: `channel list`, `channel join` (covered by [MT-CHANNEL-001](MT-CHANNEL-001.md)).
`channel watch` (covered by [MT-CHANNEL-003](MT-CHANNEL-003.md)). End-to-end agent reaction
to mentions — the orchestrator's channel router fans messages to agent inboxes, but the
agent's actual reply behaviour is RFC 0017 territory and not asserted here.

---

## Related Documentation

- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) §C, §F
- [cli/src/commands/channel.rs](../../cli/src/commands/channel.rs)
- [cli/src/commands/channel_dispatch.rs](../../cli/src/commands/channel_dispatch.rs)
- [cli/src/commands/channel_types.rs](../../cli/src/commands/channel_types.rs)
- [internal/server/channel_handlers.go](../../internal/server/channel_handlers.go)

**Related Automated Tests**:
- `cli/src/commands/channel_tests.rs` — `expand_mentions` (sender exclusion, dedup, order),
  `validate_message_id`, `WatchState::apply_batch`
- `cli/src/commands/channel_types.rs` — `PublishMessageRequest` /
  `ChannelMessage` serde-contract
- `internal/server/channel_handlers_test.go` — server-side publish, history, thread

---

## Preconditions

Same as [MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions). This test
**depends on MT-CHANNEL-001** having created `group:mt-channel-001` with members
`alice`, `bob`, `carol`, `dave`. Run them in order, or recreate the channel via the same
REST call documented there.

---

## Test Procedure

### Step 1: Plain `send` and explicit `--mention`

**Action**:

```pwsh
# 1a — top-level message, no mentions
./bin/persatrix.exe channel send mt-channel-001 "hello channel" --as alice

# 1b — repeatable --mention flag
./bin/persatrix.exe channel send mt-channel-001 "ping bob" --as alice --mention bob
```

**Expected**:
- 1a: `Sent <uuid> to #group:mt-channel-001`, exit 0.
- 1b: same shape, exit 0. The stored message's `mentions` array is `["bob"]`
  (verify via `Invoke-RestMethod`, see Step 5).

**Verification**:
- [ ] Both invocations exit 0 with a UUID-shaped message id in stdout.

---

### Step 2: `--mention-all` (client-side resolution + sender exclusion)

`--mention-all` is resolved client-side by `cmd_channel_send` via
`fetch_channel_members` (`GET /api/v1/channels/{id}`), per RFC 0011 PR-plan PR 6.
The sender is dropped — the channel gate would only fan the message back to the
same actor.

**Action**:

```pwsh
./bin/persatrix.exe channel send mt-channel-001 "everyone except me" `
    --as alice --mention-all
```

**Expected**:
- Exit 0 with the usual `Sent <uuid> …` line.
- Server stores `mentions: ["bob","carol","dave"]` — alice (the sender) is omitted
  even though she is a channel member.

**Verification**:
- [ ] After the send, `Invoke-RestMethod
  "http://127.0.0.1:8080/api/v1/channels/group:mt-channel-001/messages?limit=1"`
  shows `mentions` of length 3 with no `alice`.

---

### Step 3: `--json` send shape (capture id for Step 4 reply)

**Action**:

```pwsh
$out      = ./bin/persatrix.exe channel send mt-channel-001 "json shape check" --as carol --json
$out
$parentId = ($out | ConvertFrom-Json).id
"parentId=$parentId"
```

**Expected**:
- stdout is a single line that parses as a `ChannelMessage`:
  `{"id":"<uuid>","channel_id":"group:mt-channel-001","sender_id":"carol","content":"json shape check","timestamp":"…","mentions":[]}`.
- Note: the field `thread_id` is **not present** when empty — serde's
  `skip_serializing_if = "String::is_empty"` strips it. That is by design and is
  what makes `validate_message_id` (Step 5) load-bearing.

**Verification**:
- [ ] `ConvertFrom-Json` succeeds and `.id` is a UUID.
- [ ] `.thread_id` is absent on the top-level send.

---

### Step 4: `reply` wires `thread_id` and the server returns a thread

**Action**:

```pwsh
./bin/persatrix.exe channel reply mt-channel-001 $parentId "threaded reply" --as bob

(Invoke-RestMethod `
    "http://127.0.0.1:8080/api/v1/channels/group:mt-channel-001/messages/$parentId/thread") `
  | ConvertTo-Json -Depth 4
```

**Expected**:
- CLI prints
  `Sent <uuid> to #group:mt-channel-001 (reply to <parentId>)` — the
  third format string in [`channel.rs`](../../cli/src/commands/channel.rs)'s
  `cmd_channel_send` (`stored.thread_id` is non-empty branch).
- `GET …/messages/{parent}/thread` returns a `messages` array containing the
  reply with `thread_id` equal to `$parentId`.

**Verification**:
- [ ] CLI output contains `(reply to <parentId>)`.
- [ ] `GET …/thread` returns at least one message and its `thread_id == $parentId`.

---

### Step 5: Empty `message_id` rejected (PR #302 finding 1 — silent-degrade guard)

clap accepts `""` (or whitespace) as a positional, and serde's
`skip_serializing_if = "String::is_empty"` would then **drop** the `thread_id`
field, silently degrading a `reply` into a top-level `send`. The CLI rejects
locally so the surprise never reaches the wire.

**Action**:

```pwsh
./bin/persatrix.exe channel reply mt-channel-001 ' ' "should be rejected" --as bob
```

**Expected**:
- stderr `error: message id must not be empty`, exit 1.
- No HTTP request is issued (verify via `docker compose logs orchestrator --tail 5`
  — no new `POST /api/v1/channels/.../messages` line appears for this attempt).

**Verification**:
- [ ] Exit code is non-zero.
- [ ] stderr literal matches `message id must not be empty`.

---

### Step 6: `history` — default, `--limit`, `--json`

**Action**:

```pwsh
# 6a — default page (newest-first, server cap = channelDefaultHistoryLimit = 50)
./bin/persatrix.exe channel history mt-channel-001

# 6b — explicit --limit narrows the page
./bin/persatrix.exe channel history mt-channel-001 --limit 2

# 6c — JSON output is a single-line array of ChannelMessage
./bin/persatrix.exe channel history mt-channel-001 --limit 1 --json
```

**Expected**:
- 6a: human rows `<timestamp>  <sender>: <content>`, newest-first
  (e.g. the Step 4 reply is the first row).
- 6b: at most 2 rows (the two newest).
- 6c: single-line JSON, parses with `ConvertFrom-Json`, length matches `--limit`.
- All three exit 0.

**Verification**:
- [ ] 6a's first row is the most recent message.
- [ ] 6b returns ≤ 2 lines.
- [ ] 6c parses as a JSON array of length 1.

---

### Step 7: Self-mention is dropped (PR #302 deep-review finding 6)

**Action**:

```pwsh
./bin/persatrix.exe channel send mt-channel-001 "self-mention test" `
    --as alice --mention alice --mention bob

(Invoke-RestMethod `
    "http://127.0.0.1:8080/api/v1/channels/group:mt-channel-001/messages?limit=1") `
  | ConvertTo-Json -Depth 4
```

**Expected**:
- Exit 0 — the CLI does **not** error on self-mention; it silently drops it.
  (The mention list is the result of `expand_mentions` which skips `m == sender_id`.)
- Stored `mentions` is `["bob"]`, not `["alice","bob"]`.

**Verification**:
- [ ] Latest message's `.mentions` is exactly `["bob"]`.

---

### Step 8: Error paths

**Action**:

```pwsh
# 8a — unknown channel: server 404 surfaces as a clean error line, no panic
./bin/persatrix.exe channel send no-such-channel "should 404" --as alice

# 8b — connection refused: clean error, no panic
./bin/persatrix.exe channel list --server http://127.0.0.1:9
```

**Expected**:
- 8a: stderr `error: 404 Not Found: channel not found`, exit 1.
- 8b: stderr `error: connection failed: error sending request for url (http://127.0.0.1:9/api/v1/channels)`, exit 1.
- Neither prints a Rust backtrace (no `panicked at`, no `unwrap`).

**Verification**:
- [ ] Both error lines match the documented prefixes.
- [ ] No Rust panic backtrace in stderr.

---

### Step 9: `--mention-all` cap (cold path — see notes)

`validate_mention_count` rejects when expansion exceeds
`MAX_MENTIONS_PER_PUBLISH = 10` so the user does not round-trip an opaque 400.
Reaching the cap requires a channel with > 10 members; in the docker-compose
stack only four agents are configured. To exercise this branch live you would
need to provision a channel with 11+ members (or stub the membership list in
`channel_tests.rs`, which is what the unit test does).

**Verification** (optional):
- [ ] If exercised, error literal is
  `mentions expanded to N ids; server caps at 10. Use explicit --mention <id> repeats.`

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Plain send + explicit `--mention` succeed | ☐ |
| 2 | `--mention-all` resolves client-side, sender excluded | ☐ |
| 3 | `--json` send returns a parseable single-line `ChannelMessage` | ☐ |
| 4 | `reply` wires `thread_id`, GET `/thread` confirms | ☐ |
| 5 | Empty `message_id` rejected client-side, no HTTP issued | ☐ |
| 6 | `history` default + `--limit` + `--json` all behave as documented | ☐ |
| 7 | Self-mention silently dropped from the stored mentions array | ☐ |
| 8 | Server 404 + connection-refused both surface as clean errors | ☐ |
| 9 | Mention-cap branch (cold path) — covered by unit test | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Long content body

The server enforces `ErrMessageContentTooLarge` → 400; the CLI just surfaces it
via `api_error_message`. Reproducing requires hitting the server limit (256 KiB
in v0.3.0) which is out of scope for this manual pass; covered by
`internal/server/channel_handlers_test.go`.

### Edge Case 2: Mention id that is not a channel member

The server **does not** reject mentions of non-members (per the channel router
fan-out semantics — mentions are routing hints, not membership constraints). The
CLI does only shape validation (`validate_resource_id`). Sending
`--mention nobody` against `mt-channel-001` is therefore expected to succeed and
store `mentions: ["nobody"]`.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-09 | Claude (PR #302 prep) | Windows 11 | Pass | Full live run on `feature/v030-rfc0011-cli-subcommands` @ `82a0602` against the docker-composed stack. Step 1 — both sends produced UUID message ids (`428585c6…`, `76341c43…`). Step 2 — `--mention-all` stored `["bob","carol","dave"]` on alice's send (sender excluded). Step 3 — `--json` returned `{"id":"92fbf7be…","channel_id":"group:mt-channel-001","sender_id":"carol","content":"json shape check","timestamp":"2026-05-09T07:41:22.369683Z","mentions":[]}` with `thread_id` field absent as expected. Step 4 — `reply` printed `Sent 022b86b0… to #group:mt-channel-001 (reply to 92fbf7be…)`; GET `/thread` returned a single message with matching `thread_id`. Step 5 — `error: message id must not be empty`, exit 1, no HTTP issued. Step 6 — default human page newest-first; `--limit 2` returned 2 rows; `--limit 1 --json` returned a parseable single-line array. Step 7 — alice's self-mention attempt stored `mentions: ["bob"]` only. Step 8 — `error: 404 Not Found: channel not found` and `error: connection failed: error sending request for url …` both clean, no panic. Step 9 — not exercised live (cold path; only 4 agents in compose). |

---

## Notes

- `expand_mentions` order is **stable**: explicit flags first in input order, then
  remaining members in server order; duplicates collapse. Tested in
  `channel_tests.rs::expand_mentions_*`.
- `cmd_channel_send` is shared by both `Send` and `Reply` dispatch arms — the only
  difference is `thread_id` (`""` for send, validated message id for reply). See
  [`channel_dispatch.rs`](../../cli/src/commands/channel_dispatch.rs).
- Server-side validation errors (`ErrInvalidChannelType`, `ErrInvalidParticipantID`,
  `ErrInvalidRespondPolicy`) all map to HTTP 400 via `writeChannelError` — the CLI
  surfaces those via `api_error_message`. Local validation in `validate_send_inputs`
  catches the common typos before that round-trip.
