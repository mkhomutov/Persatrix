# Manual Test MT-CHANNEL-005: DM canonicalization round-trip

**Test ID**: `MT-CHANNEL-005`
**Feature Area**: Channels (DM identity)
**Version**: 2.0
**Created**: 2026-05-09
**Last Updated**: 2026-05-09
**Status**: Active

> **v2.0 rewrite (2026-05-09)** — the original v1.0 procedure assumed a
> `POST /api/v1/channels` DM-creation surface that does not exist in v0.3.0
> (`createChannelRequest` carries only `{name, description, members}` and the
> handler unconditionally derives `group:<name>`; see §Out of Scope below).
> v2.0 drives the only v0.3.0 REST surface that reaches `GetOrCreateDM` —
> the chat handler — and turns the absence of the alternate surfaces into a
> first-class invariant assertion.

---

## Overview

**Purpose**: Verify the DM canonicalization invariant from RFC 0011 §A end-to-end:
the lex-sorted `dm:<min>:<max>` channel id is the database PK regardless of
which side of the DM initiates, and once the row exists either member can
publish into it via REST and history aggregates under that single id.

**Scope**: `POST /api/v1/agents/{id}/chat` to drive `GetOrCreateDM` (the only
public REST surface that creates DM channels in v0.3.0 —
[chat_handler.go:199](../../internal/server/chat_handler.go#L199)),
`GET /api/v1/channels/{id}` to confirm the canonical id, paired
`POST /api/v1/channels/{id}/messages` from each member to confirm both
directions land in the same row, and `GET /api/v1/channels/{id}/messages`
to confirm history aggregates.

**Out of Scope**:

- DM creation via `POST /api/v1/channels`. v0.3.0 has no such surface:
  `createChannelRequest`
  ([channel_types.go:10-14](../../internal/server/channel_types.go#L10-L14))
  carries only `{name, description, members}` and `handleCreateChannel`
  unconditionally derives `canonicalID := "group:" + req.Name` and sets
  `Type: ChannelTypeGroup`
  ([channel_handlers.go:101-105](../../internal/server/channel_handlers.go#L101-L105)).
  Adding a DM branch to that handler is tracked as a v0.3.x follow-up.
- The publish path also does **not** auto-create DMs:
  [sqlite_messages.go:97-111](../../internal/channels/sqlite_messages.go#L97-L111)
  runs the membership probe first and surfaces `ErrChannelNotFound` (→ 404)
  when the row is missing. Server-side canonicalization is reached **only**
  from the chat handler in v0.3.0.
- CLI `channel send` for DMs: same wire surface as REST publish; the
  canonicalization happened server-side at chat time. Symmetry of
  `CanonicalDMID` is unit-pinned by `TestCanonicalDMID_SortsParticipants`
  ([sqlite_test.go:57](../../internal/channels/sqlite_test.go#L57)).

---

## Related Documentation

- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) §A
  ("DM canonicalization"), §C (REST endpoints)
- [internal/channels/identifiers.go](../../internal/channels/identifiers.go)
  — `validateParticipantID` ([line 167](../../internal/channels/identifiers.go#L167))
  + `CanonicalDMID` ([line 185](../../internal/channels/identifiers.go#L185))
- [internal/server/chat_handler.go:199](../../internal/server/chat_handler.go#L199)
  — only REST entry point that calls `GetOrCreateDM` in v0.3.0

**Related Automated Tests**:

- `internal/channels/sqlite_test.go::TestCanonicalDMID_SortsParticipants`
  ([sqlite_test.go:57](../../internal/channels/sqlite_test.go#L57)) —
  symmetric pair lex sort.
- `internal/channels/sqlite_test.go::TestCanonicalDMID_RejectsInvalid`
  ([sqlite_test.go:66](../../internal/channels/sqlite_test.go#L66)) —
  distinct-participants check + id-pattern rejection.
- `internal/channels/sqlite_test.go::TestSQLiteStore_GetOrCreateDM_Idempotent`
  ([sqlite_test.go:297](../../internal/channels/sqlite_test.go#L297)) —
  idempotent get-or-create across reopen.
- `internal/channels/sqlite_test.go::TestSQLiteStore_GetOrCreateDM_ConcurrentRace`
  ([sqlite_test.go:427](../../internal/channels/sqlite_test.go#L427)) —
  concurrent caller race resolution.
- `internal/server/chat_handler_review_test.go::TestHandleChat_GetOrCreateDM_ValidationErrorStillReturns400`
  ([chat_handler_review_test.go:170](../../internal/server/chat_handler_review_test.go#L170))
  — same-participant rejection at the chat boundary.

---

## Preconditions

Same as [MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions),
**plus**:

- ☐ `.env` carries a valid `ANTHROPIC_API_KEY`. Steps 1, 5, 6, 7 dispatch
  through the chat handler which awaits a real persona reply.
- ☐ Default `config/agents.yaml` (with `ember-owl` declared as a persona) —
  no edits needed; `ember-owl` is the agent counterparty in every step.

This test is independent of MT-CHANNEL-001/002/003/004/006. Stale DM rows
from prior chat MTs are tolerated — the create path is idempotent and the
test asserts only on its own observable canonical ids.

---

## Test Procedure

### Step 1: Drive DM creation by chatting from `alice` to `ember-owl`

In v0.3.0 the chat handler is the only REST surface that reaches
`GetOrCreateDM`. A successful chat materialises the DM row and inserts
both participants as members
([chat_handler.go:199](../../internal/server/chat_handler.go#L199)).

**Action**:

```pwsh
$body = '{"message":"hi from MT-005","user_id":"alice","participant_type":"user"}'
$resp = Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/agents/ember-owl/chat `
    -Method POST -ContentType 'application/json' -Body $body -TimeoutSec 60
$resp | Select-Object reply_status, agent_id, chat_session_id | Format-Table
```

**Expected**:
- HTTP 200; `$resp.reply_status == "ok"` and `$resp.reply` is non-empty
  (LLM-generated).

**Verification**:
- [ ] `$resp.reply_status` is `"ok"`.
- [ ] `$resp.reply` is a non-empty string.

---

### Step 2: Confirm the canonical channel id is `dm:alice:ember-owl`

Lex sort puts `alice` before `ember-owl` (ASCII `a` < `e`), so the canonical
id is `dm:alice:ember-owl` regardless of the `(userID, agentID)` argument
order in the chat handler's `GetOrCreateDM(userID, agentID)` call.

**Action**:

```pwsh
$dm = Invoke-RestMethod http://127.0.0.1:8080/api/v1/channels/dm:alice:ember-owl
$dm | ConvertTo-Json -Depth 4
```

**Expected**:
- `$dm.id == "dm:alice:ember-owl"`.
- `$dm.channel_type == "dm"`.
- `$dm.members` contains both `alice` and `ember-owl`.

**Verification**:
- [ ] `$dm.id` is exactly `dm:alice:ember-owl`.
- [ ] `$dm.channel_type` is `dm`.
- [ ] Member id set is `{alice, ember-owl}`.

---

### Step 3: Publish from `alice` into the canonical DM via REST

Once the DM row exists, both members can publish into it through the
standard publish endpoint — server-side canonicalization is not re-applied
because the caller already addresses the canonical id directly.

**Action**:

```pwsh
$msgA = '{"sender_id":"alice","content":"REST publish from alice"}'
$idA = (Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels/dm:alice:ember-owl/messages `
    -Method POST -ContentType 'application/json' -Body $msgA).id
"alice msg id=$idA"
```

**Expected**:
- 201 Created with a UUID `id`.

**Verification**:
- [ ] `$idA` is a UUID-shaped string.

---

### Step 4: Publish from `ember-owl` into the same canonical DM

Symmetric to Step 3 — same channel id, opposite sender. This is the
end-to-end demonstration that both DM members reach the same row by the
canonical id alone.

**Action**:

```pwsh
$msgB = '{"sender_id":"ember-owl","content":"REST publish from ember-owl"}'
$idB = (Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels/dm:alice:ember-owl/messages `
    -Method POST -ContentType 'application/json' -Body $msgB).id
"ember-owl msg id=$idB"
```

**Expected**:
- 201 Created with a UUID.

**Verification**:
- [ ] `$idB` is a UUID-shaped string and `$idB -ne $idA`.

---

### Step 5: History aggregates everything under the canonical id

The chat round-trip in Step 1 plus the two REST publishes in Steps 3-4
should all live under `channel_id == "dm:alice:ember-owl"`.

**Action**:

```pwsh
$hist = Invoke-RestMethod `
    "http://127.0.0.1:8080/api/v1/channels/dm:alice:ember-owl/messages?limit=20"
$hist.messages |
    Select-Object sender_id, channel_id, content |
    Format-Table -AutoSize
"count=$($hist.messages.Count)"
```

**Expected**:
- At least 4 rows: chat-inbound from `alice`, chat-reply from `ember-owl`,
  REST publish from `alice`, REST publish from `ember-owl`.
- Every row has `channel_id == "dm:alice:ember-owl"`.

**Verification**:
- [ ] Every `channel_id` is `dm:alice:ember-owl` — no rows leak to a
  different id (e.g., `dm:ember-owl:alice` would prove canonicalization
  broken).
- [ ] Both REST publish ids from Steps 3-4 (`$idA`, `$idB`) appear in the
  history.

---

### Step 6: Lex-sort symmetry — chat from `zara` to `ember-owl` lands in `dm:ember-owl:zara`

`alice < ember-owl` exercised the "user-before-agent" half of the sort.
Picking a user whose id sorts **after** the agent (`zara > ember-owl`)
exercises the "user-after-agent" half. `CanonicalDMID` swaps internally —
the chat-handler arg order `(userID, agentID)` is irrelevant to the
resulting channel id.

**Action**:

```pwsh
$body = '{"message":"hi from MT-005 step 6","user_id":"zara","participant_type":"user"}'
$null = Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/agents/ember-owl/chat `
    -Method POST -ContentType 'application/json' -Body $body -TimeoutSec 60
$dmZ = Invoke-RestMethod http://127.0.0.1:8080/api/v1/channels/dm:ember-owl:zara
$dmZ.id
```

**Expected**:
- `$dmZ.id == "dm:ember-owl:zara"` (lex sort: `ember-owl` < `zara`).
- A `GET` for the swapped id `dm:zara:ember-owl` would return 404 — that
  row does not exist by construction.

**Verification**:
- [ ] `$dmZ.id` is exactly `dm:ember-owl:zara`.
- [ ] (Optional) `Invoke-WebRequest http://127.0.0.1:8080/api/v1/channels/dm:zara:ember-owl`
      throws with `StatusCode == 404`.

---

### Step 7: Same-participant DM is rejected at the chat boundary

`CanonicalDMID` enforces "two distinct participants"
([identifiers.go](../../internal/channels/identifiers.go#L192-L194)).
The chat handler maps `ErrInvalidParticipantID` to 400
([chat_handler.go:245-249](../../internal/server/chat_handler.go#L245-L249)).
Send a chat where `user_id` equals the agent id to exercise this path.

> The 400 fires from the `CanonicalDMID` same-id check and is independent of
> *which* valid `participant_type` you send — the chat handler propagates that
> field as metadata only. Flipping the body between the two valid values
> (`"agent"` ↔ `"user"`) does not change the outcome; the rejection is purely
> id-shape. (An *out-of-vocabulary* `participant_type` such as `"human"` is
> itself rejected with 400 earlier, by the ISSUE-0068 validation, before the
> id-shape check — so use a valid value here to exercise the same-id path.)

**Action**:

```pwsh
$body = '{"message":"self-DM rejected","user_id":"ember-owl","participant_type":"agent"}'
try {
  Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/agents/ember-owl/chat `
      -Method POST -ContentType 'application/json' -Body $body
} catch {
  $_.Exception.Response.StatusCode.Value__
  $_.ErrorDetails.Message
}
```

**Expected**:
- HTTP 400; error envelope mentions `invalid participant id`.

**Verification**:
- [ ] StatusCode is `400`.
- [ ] Error code is `BAD_REQUEST` and the `error` string contains
      `invalid participant id`.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Chat from `alice` to `ember-owl` returns `reply_status=ok` | ☐ |
| 2 | `GET /channels/dm:alice:ember-owl` returns the canonical id + both members | ☐ |
| 3 | REST publish from `alice` into the DM returns 201 | ☐ |
| 4 | REST publish from `ember-owl` into the DM returns 201 | ☐ |
| 5 | History under `dm:alice:ember-owl` aggregates ≥ 4 rows; all share the canonical id | ☐ |
| 6 | Chat from `zara` produces `dm:ember-owl:zara` (sort is symmetric) | ☐ |
| 7 | Chat with `user_id == agent_id` is rejected with 400 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Case-sensitive participant ids — `Alice` vs `alice` are distinct

The participant-id pattern is permissive on case:
`participantIDPattern = ^[A-Za-z0-9][A-Za-z0-9_-]*$`
([identifiers.go](../../internal/channels/identifiers.go#L150)). Both `Alice`
and `alice` pass `validateParticipantID`, so a chat from `user_id: "Alice"`
materialises a **separate** DM row at `dm:Alice:alice` — capital letters
sort before lowercase in ASCII (`A` = 0x41 < `a` = 0x61), so `Alice` is
the canonical `min`.

This is by design: there is no case-folding logic in `CanonicalDMID`
([identifiers.go](../../internal/channels/identifiers.go#L185-L199)), and
`participantIDPattern` is the single source of truth across schema, loader,
and runtime ([identifiers.go](../../internal/channels/identifiers.go#L137-L150)).
The `^[a-z0-9][a-z0-9-]*[a-z0-9]$` pattern that *is* lowercase-only is
`channelNamePattern` ([identifiers.go](../../internal/channels/identifiers.go#L162))
— that one applies to group-channel **names** (`group:<name>`), not to
participant ids.

Operators choosing user ids should treat case as significant: `Alice` and
`alice` produce distinct chat histories, distinct relationship-memory rows,
and distinct DM channels. If accidental drift is a concern, normalise at
the caller before posting.

### Edge Case 2: Stale DM channels from prior runs

The `dm:alice:ember-owl` row may already exist from earlier MT-CHAT or
MT-CHANNEL runs. `GetOrCreateDM` is idempotent — the chat in Step 1 simply
returns the existing row, and the publishes in Steps 3-4 land in the same
channel. The test asserts only on its own newly-published REST messages
(`$idA`, `$idB`) and on the canonical id, not on row count totals.

### Edge Case 3: User membership reset to `RespondNever` after chat

The chat handler demotes the user's membership to `RespondNever` on every
turn ([chat_handler.go:212-219](../../internal/server/chat_handler.go#L212-L219))
to prevent the dispatcher from emitting a per-reply WARN ("dispatch target
not registered") for the human-in-the-DM. This is invisible to MT-005 —
publishes from `alice` via REST in Step 3 still succeed because membership
exists; only the response gate is suppressed for `alice` as a recipient.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|

---

## Notes

- The canonical DM id format `dm:<min>:<max>` is RFC 0011 §A; this MT pins
  the *behavior* (lex-sorted, distinct-participants, idempotent), not the
  format string. If the format ever changes, this test must change with it.
- DM membership is implicit — there is no `POST /channels/{id}/members`
  call required after the chat materialises the row. Both participants
  are inserted by `GetOrCreateDM` at create time
  ([sqlite.go GetOrCreateDM implementation](../../internal/channels/sqlite.go))
  with `RespondPolicy = always` initially; the chat handler then demotes
  the user side to `RespondNever` per Edge Case 3.
- Steps 1, 6, 7 each cost one chat round-trip (≤ 1 LLM call apiece). The
  full procedure costs ~3 LLM calls; budget accordingly when running
  against a paid endpoint.
