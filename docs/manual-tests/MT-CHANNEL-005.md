# Manual Test MT-CHANNEL-005: DM canonicalization round-trip

**Test ID**: `MT-CHANNEL-005`
**Feature Area**: Channels (DM identity)
**Version**: 1.0
**Created**: 2026-05-09
**Last Updated**: 2026-05-09
**Status**: Active

---

## Overview

**Purpose**: Verify the DM canonicalization invariant from RFC 0011 §A end-to-end:
publishing a DM message with the participant pair `(alice, bob)` and `(bob, alice)`
both resolve to the same canonical channel id `dm:alice:bob`, and the resulting
history contains both messages in chronological order regardless of submission
order. The `ChannelStore.GetOrCreateDM` helper is the single source of truth for
this canonicalization (lexicographic sort, `:`-joined) — this MT exercises the
contract through the public REST surface.

**Scope**: `POST /api/v1/channels` with `channel_type: "dm"` body, two
publishes via `POST /api/v1/channels/dm:alice:bob/messages` from each side, and
`GET /api/v1/channels/{id}/messages` to confirm both land in the same channel.

**Out of Scope**: CLI `channel send` for DMs is the same wire surface — the
canonicalization happens server-side regardless of caller; covered by
`internal/channels/sqlite_test.go::TestSQLiteStore_GetOrCreateDM_Symmetric`.
Cross-membership-flow gating (DM members are implicit; no `AddMember`
required) is out of scope here — see RFC 0011 §A.

---

## Related Documentation

- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) §A
  ("DM canonicalization"), §C (REST endpoints)
- [internal/channels/channels.go](../../internal/channels/channels.go)
  — `validateParticipantID` + `GetOrCreateDM` ID derivation
- [internal/channels/sqlite.go](../../internal/channels/sqlite.go)
  — `GetOrCreateDM` SQLite implementation (lexicographic sort)

**Related Automated Tests**:
- `internal/channels/sqlite_test.go::TestSQLiteStore_GetOrCreateDM_*` —
  symmetric pair, distinct-participants validation, DM ID stability across
  reopen
- `internal/server/channel_handlers_test.go` — REST publish DM round-trip

---

## Preconditions

Same as [MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions). This
test does **not** require an `ANTHROPIC_API_KEY` — no agent reply is asserted;
the orchestrator's REST surface alone is exercised. Existing DM channels from
prior MT runs are tolerated; the test creates a fresh pair and asserts only on
its own messages.

---

## Test Procedure

### Step 1: Create the DM channel via REST

DMs auto-resolve via `GetOrCreateDM` on first publish. To make the canonical
id observable up-front, the test creates the channel explicitly before
publishing.

**Action**:

```pwsh
$body = '{"channel_type":"dm","members":[{"id":"alice"},{"id":"bob"}]}'
$resp = Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels `
    -Method POST -ContentType 'application/json' -Body $body
$resp | ConvertTo-Json -Depth 4
```

**Expected**:
- Response has `id == "dm:alice:bob"` (lexicographic sort: `a` < `b`).
- `channel_type == "dm"`; `members` has both ids.

**Verification**:
- [ ] `$resp.id` is exactly `dm:alice:bob`.

---

### Step 2: Publish from `alice` to `bob`

**Action**:

```pwsh
$msgA = '{"sender_id":"alice","content":"hello bob, this is alice"}'
(Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels/dm:alice:bob/messages `
    -Method POST -ContentType 'application/json' -Body $msgA).id
```

**Expected**:
- Returns a UUID string.
- Server stores the row with `channel_id = "dm:alice:bob"` and
  `sender_id = "alice"`.

**Verification**:
- [ ] Response is a UUID.

---

### Step 3: Publish from `bob` back to `alice` — same canonical channel

**Action**:

```pwsh
$msgB = '{"sender_id":"bob","content":"hi alice, got your note"}'
(Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels/dm:alice:bob/messages `
    -Method POST -ContentType 'application/json' -Body $msgB).id
```

**Expected**:
- Returns a UUID; second message persisted under the same `channel_id`.

**Verification**:
- [ ] Response is a UUID.

---

### Step 4: Confirm both messages live in the same channel

**Action**:

```pwsh
$hist = Invoke-RestMethod `
    "http://127.0.0.1:8080/api/v1/channels/dm:alice:bob/messages?limit=10"
$hist.messages | Select-Object sender_id, content, channel_id | Format-Table
```

**Expected**:
- Two rows present (newest-first):
  - `bob` / `hi alice, got your note` / `channel_id = dm:alice:bob`
  - `alice` / `hello bob, this is alice` / `channel_id = dm:alice:bob`

**Verification**:
- [ ] Both messages have `channel_id == "dm:alice:bob"`.
- [ ] `sender_id` values cover both `alice` and `bob` exactly once each.

---

### Step 5: Reverse-order create is idempotent (canonical id stable)

`POST /api/v1/channels` with the participant order swapped resolves to the
**same** row already created in Step 1 — the canonical id is the database PK,
so re-create is a no-op.

**Action**:

```pwsh
$bodyReversed = '{"channel_type":"dm","members":[{"id":"bob"},{"id":"alice"}]}'
$resp2 = Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels `
    -Method POST -ContentType 'application/json' -Body $bodyReversed
$resp2.id
$resp2.created_at -eq $resp.created_at
```

**Expected**:
- `$resp2.id == "dm:alice:bob"` (same id, regardless of input order).
- `created_at` matches the Step 1 timestamp — no new row was inserted.

**Verification**:
- [ ] `$resp2.id` equals `dm:alice:bob`.
- [ ] `created_at` equality check returns `True`.

---

### Step 6: Edge — DM with two equal participants is rejected

`GetOrCreateDM` returns `ErrInvalidParticipantID: dm requires two distinct
participants` on `(a, a)`. The REST handler maps that to HTTP 400.

**Action**:

```pwsh
$bodySame = '{"channel_type":"dm","members":[{"id":"alice"},{"id":"alice"}]}'
try {
  Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels `
      -Method POST -ContentType 'application/json' -Body $bodySame
} catch {
  $_.Exception.Response.StatusCode.Value__
  ($_.ErrorDetails.Message)
}
```

**Expected**:
- HTTP 400, error body mentions `invalid participant id` and the duplicate id.

**Verification**:
- [ ] StatusCode is 400.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Create returns `dm:alice:bob` (sorted) | ☐ |
| 2 | Publish from alice succeeds | ☐ |
| 3 | Publish from bob succeeds | ☐ |
| 4 | History shows both messages under the canonical id | ☐ |
| 5 | Reverse-order create is idempotent (same id, same `created_at`) | ☐ |
| 6 | Same-participant DM rejected with 400 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Lexicographic edge — IDs differing only in case

`validateParticipantID` rejects uppercase via the `^[a-z0-9][a-z0-9-]*[a-z0-9]$`
pattern enforced at registration ([internal/channels/channels.go](../../internal/channels/channels.go)),
so `(Alice, alice)` fails before reaching `GetOrCreateDM`. No additional
case-folding logic is needed in the canonicalization layer.

### Edge Case 2: Stale DM channels from prior runs

The `dm:alice:bob` row may already exist from earlier test sessions (or from
MT-CHAT-001 if `alice`/`bob` were the chat participants). Step 1's create is
idempotent — if the row exists, the response is HTTP 200 and the existing row
is returned. The test asserts only on its own newly-published messages.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|

---

## Notes

- The canonical DM id format `dm:<min>:<max>` is RFC 0011 §A; the test pins
  the *behavior*, not the format string. If the format ever changes, this
  test must change with it.
- DM membership is implicit — there is no `POST /channels/{id}/members` call
  required after the channel exists. Both participants are members by
  construction; the membership row is inserted by `GetOrCreateDM` at create
  time.
