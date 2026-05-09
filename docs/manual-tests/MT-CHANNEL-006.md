# Manual Test MT-CHANNEL-006: Channel deletion + cascade

**Test ID**: `MT-CHANNEL-006`
**Feature Area**: Channels (lifecycle)
**Version**: 1.0
**Created**: 2026-05-09
**Last Updated**: 2026-05-09
**Status**: Active

---

## Overview

**Purpose**: Verify the two channel DELETE endpoints from RFC 0011 §C end-to-end:

1. `DELETE /api/v1/channels/{id}` removes the channel and **cascades** to its
   memberships and messages (RFC 0011 §B "Channel-deletion cascade", enforced
   by `PRAGMA foreign_keys = ON` + the `ON DELETE CASCADE` schema).
2. `DELETE /api/v1/channels/{id}/members/{participant_id}` removes a single
   membership row but **preserves** the participant's prior messages
   ([internal/server/channel_delete_handlers.go](../../internal/server/channel_delete_handlers.go)).

**Scope**: REST DELETE surface for channel and membership; subsequent 404 on
unknown channel; behaviour after partial removal of a member with prior
history.

**Out of Scope**: CLI surface — no `persatrix channel delete` subcommand
exists in v0.3.0 (RFC 0011 §F lists `list/join/send/reply/history/watch`).
Operators delete via REST. The thread-FK cascade boundary case (deleting a
channel with a thread root straddling the per-channel cap) is covered by
`internal/channels/sqlite_test.go::TestSQLiteStore_ThreadFKCascade`.

---

## Related Documentation

- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) §B
  ("Channel-deletion cascade"), §C (REST endpoint table)
- [internal/server/channel_delete_handlers.go](../../internal/server/channel_delete_handlers.go)
- [internal/channels/sqlite.go](../../internal/channels/sqlite.go) —
  `DeleteChannel`, `RemoveMember`

**Related Automated Tests**:
- `internal/channels/sqlite_test.go::TestSQLiteStore_ChannelDeletionCascade`
  ([sqlite_test.go:272](../../internal/channels/sqlite_test.go#L272)) — store-level cascade semantics
- `internal/channels/sqlite_test.go::TestSQLiteStore_ThreadFKCascade`
  ([sqlite_test.go:233](../../internal/channels/sqlite_test.go#L233)) — thread-FK cascade boundary
- `internal/server/channel_handlers_test.go` — REST surface, five functions:
  `TestChannels_DeleteChannel_CascadesMembershipsAndMessages`
  ([:332](../../internal/server/channel_handlers_test.go#L332)),
  `TestChannels_DeleteChannel_NotFound`
  ([:372](../../internal/server/channel_handlers_test.go#L372)),
  `TestChannels_DeleteMember_PreservesPriorMessages`
  ([:381](../../internal/server/channel_handlers_test.go#L381)),
  `TestChannels_DeleteMember_404OnUnknownChannel`
  ([:419](../../internal/server/channel_handlers_test.go#L419)),
  `TestChannels_DeleteMember_404OnUnknownMember`
  ([:428](../../internal/server/channel_handlers_test.go#L428))

---

## Preconditions

Same as [MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions). This
test does **not** require an `ANTHROPIC_API_KEY` — pure REST exercise; no LLM
round-trip. The test creates and tears down its own channel and is independent
of MT-CHANNEL-001..005.

---

## Test Procedure

### Step 1: Create a group channel and seed it with messages

**Action**:

```pwsh
$body = @'
{
  "name":"mt-channel-006",
  "members":[
    {"id":"alice","respond":"when_mentioned"},
    {"id":"bob","respond":"when_mentioned"}
  ]
}
'@
$ch = Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels `
    -Method POST -ContentType 'application/json' -Body $body
"created $($ch.id)"

# Two messages so we can verify the cascade later
$null = Invoke-RestMethod `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006/messages `
    -Method POST -ContentType 'application/json' `
    -Body '{"sender_id":"alice","content":"first"}'
$null = Invoke-RestMethod `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006/messages `
    -Method POST -ContentType 'application/json' `
    -Body '{"sender_id":"bob","content":"second"}'

$hist = Invoke-RestMethod `
    "http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006/messages?limit=10"
"messages_before_delete=$($hist.messages.Count)"
```

**Expected**:
- Channel created with id `group:mt-channel-006`.
- Both messages persist; history count is `2`.

**Verification**:
- [ ] `messages_before_delete` is `2`.

---

### Step 2: Remove a single member — prior messages preserved

`DELETE /api/v1/channels/{id}/members/{participant_id}` removes the membership
row only; `messages.sender_id` retains the historical id (RFC 0011 §C
endpoint table).

**Action**:

```pwsh
(Invoke-WebRequest -Method DELETE `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006/members/bob `
    -UseBasicParsing).StatusCode

$ch = Invoke-RestMethod http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006
$ch.members | Select-Object id, respond_policy | Format-Table

$hist = Invoke-RestMethod `
    "http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006/messages?limit=10"
$hist.messages | Select-Object sender_id, content | Format-Table
```

**Expected**:
- HTTP 204 No Content from the DELETE.
- `members` array now has one entry only (`alice`); `bob` is gone.
- History still shows both messages (`alice`/`first` + `bob`/`second`); the
  `bob`-authored message is preserved despite the membership removal.

**Verification**:
- [ ] Member count is now `1`; only `alice` remains.
- [ ] History count is still `2`; the `bob`/`second` row is present.

---

### Step 3: Remove a non-member — 404

The `ErrNotMember` sentinel maps to HTTP 404 in this DELETE context per the
[handler comment](../../internal/server/channel_delete_handlers.go) — "no row
to remove" is the correct semantics, not the publish-side 403.

**Action**:

```pwsh
try {
  Invoke-WebRequest -Method DELETE `
      -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006/members/ghost `
      -UseBasicParsing
} catch {
  $_.Exception.Response.StatusCode.Value__
}
```

**Expected**:
- HTTP 404.

**Verification**:
- [ ] StatusCode is `404`.

---

### Step 4: Delete the channel — cascade fires

`DELETE /api/v1/channels/{id}` cascades to memberships and messages via the
SQLite `ON DELETE CASCADE` rules
([internal/channels/sqlite.go](../../internal/channels/sqlite.go)).

**Action**:

```pwsh
(Invoke-WebRequest -Method DELETE `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006 `
    -UseBasicParsing).StatusCode
```

**Expected**:
- HTTP 204 No Content.

**Verification**:
- [ ] StatusCode is `204`.

---

### Step 5: Confirm cascade — channel, members, and messages all gone

**Action**:

```pwsh
# 5a — channel itself returns 404
try {
  Invoke-WebRequest http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006 `
      -UseBasicParsing
} catch { $_.Exception.Response.StatusCode.Value__ }

# 5b — history under the now-deleted id returns 404
try {
  Invoke-WebRequest "http://127.0.0.1:8080/api/v1/channels/group:mt-channel-006/messages?limit=10" `
      -UseBasicParsing
} catch { $_.Exception.Response.StatusCode.Value__ }

# 5c — list no longer shows the channel
./bin/persatrix.exe channel list | Select-String "mt-channel-006"
```

**Expected**:
- 5a: 404.
- 5b: 404 — message rows for the deleted channel are gone (cascade);
  separately, the handler short-circuits to 404 once the parent row is missing.
- 5c: no output (the channel is no longer in `list`).

**Verification**:
- [ ] 5a + 5b both return 404.
- [ ] 5c emits no matching line.

---

### Step 6: DELETE on an unknown channel is 404

**Action**:

```pwsh
try {
  Invoke-WebRequest -Method DELETE `
      -Uri http://127.0.0.1:8080/api/v1/channels/group:never-existed `
      -UseBasicParsing
} catch { $_.Exception.Response.StatusCode.Value__ }
```

**Expected**:
- HTTP 404 — the handler maps `ErrChannelNotFound` to 404.

**Verification**:
- [ ] StatusCode is `404`.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Channel created with two members + two messages | ☐ |
| 2 | DELETE member removes membership but preserves prior messages | ☐ |
| 3 | DELETE non-member returns 404 | ☐ |
| 4 | DELETE channel returns 204 | ☐ |
| 5 | Channel, history, and `channel list` all reflect the cascade | ☐ |
| 6 | DELETE on unknown channel returns 404 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Concurrent delete + publish

If a publish request is in flight when the channel is deleted, the publish
returns the standard error mapping (`ErrChannelNotFound` → 404 or
`ErrNotMember` → 403 depending on which check fires first). The race is not
asserted live in this MT — the boundary is small and dominated by the
parent-row existence check. There is no dedicated automated race test for
this seam in v0.3.0; the per-row probe-then-write inside `PublishMessage`
([sqlite_messages.go:97-111](../../internal/channels/sqlite_messages.go#L97-L111))
serializes through SQLite's per-write lock so the lost-write window is
bounded by a single transaction. Tracked as a v0.3.x follow-up if a flake
ever surfaces.

### Edge Case 2: Delete with thread roots that straddle the cap

The thread-FK cascade case (deleting a channel whose oldest message is the
root of a thread that pruned to past the per-channel cap) is covered by
`TestSQLiteStore_ThreadFKCascade`. Reproducing live requires publishing 10,001
messages, which is impractical for a manual pass; treat that boundary as
covered by the unit test.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|

---

## Notes

- DELETE endpoints are unauthenticated in v0.3.0 (RFC 0011 §C trust
  boundary; token auth lands in RFC 0009 Phase 4). Operators on shared
  networks must front the orchestrator with their own auth/ingress until
  v0.4.0.
- There is intentionally no CLI for delete — the operation is rare and
  destructive enough to live behind explicit `Invoke-WebRequest -Method DELETE`
  / `curl -X DELETE` until the auth story matures.
