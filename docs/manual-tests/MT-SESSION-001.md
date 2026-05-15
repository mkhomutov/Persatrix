# Manual Test MT-SESSION-001: PERSATRIX_SESSION_ID cross-process write contract

**Test ID**: `MT-SESSION-001`
**Feature Area**: Sessions (RFC 0031 Phase 1 — namespace + env-var threading)
**Version**: 1.0
**Created**: 2026-05-13
**Last Updated**: 2026-05-13
**Status**: Active

---

## Overview

**Purpose**: Verify that `PERSATRIX_SESSION_ID` set in the operator's
environment is read at orchestrator + persona-runtime boot and stamped on
every storage row those processes write — channels.db on the Go side
(`channels.session_id`, `messages.session_id`) and memory.db on the
Python side (`episodes.session_id`, `relationships.session_id`).

This is the Phase 1 acceptance walkthrough for RFC 0031 — the storage
half of per-session namespacing. Phase 1 ships **no recall-side
filtering**; this test only asserts the *write* contract.

**Scope**:
- Two stack starts under different `PERSATRIX_SESSION_ID` values
  (`run-a`, `run-b`).
- After each start, write a channel + message and trigger a persona
  episode + relationship-row write.
- Open the SQLite files directly and verify both runs' rows exist,
  each tagged with the matching `session_id`.
- The synthetic `legacy` carve-out (RFC 0031 OQ #2) is applied when
  the env var is unset.

**Out of Scope**:
- Phase 2 recall filtering (per-session episodic / relationship recall
  shape) — lands in a later v0.3.x patch.
- Phase 3 CLI (`persatrix session new / use / list / archive / current`,
  `--session` flag) — lands later.
- Phase 4 operator documentation pass (`docs/guides/sessions.md`).
- Cross-binary integration is exercised here; the within-process
  storage contract is covered by automated tests:
  - `tests/unit/python/test_session_id_migration.py` (schema v7)
  - `tests/unit/python/test_session_id_writes.py` (kwarg round-trip)
  - `tests/unit/python/test_session_id_persona_runtime.py` (env read +
    threading)
  - `tests/integration/test_session_id_cross_process.py` (Python
    subprocess env → storage)
  - `internal/channels/sqlite_session_migration_test.go` (channels.db
    v2 → v3)
  - `internal/channels/sqlite_session_test.go` (Go write-path)
  - `cmd/orchestrator/session_env_test.go` (Go env resolution).

---

## Related Documentation

- [docs/rfcs/0031-per-session-namespacing-channels.md](../rfcs/0031-per-session-namespacing-channels.md)
  — canonical spec (Phase 1 §D pseudocode).
- [docs/rfcs/0031-pr-plan.md](../rfcs/0031-pr-plan.md) — PR sequence;
  this MT is the Phase 4 PR 1 (release-prep execution) deliverable
  authored as part of PR 3.
- [internal/channels/sqlite_schema.go](../../internal/channels/sqlite_schema.go)
  — channels.db v3 migration (orchestrator side).
- [agents/memory/_migration_handlers.py](../../agents/memory/_migration_handlers.py)
  — memory.db v7 migration (Python side).
- [cmd/orchestrator/startup.go](../../cmd/orchestrator/startup.go) —
  `resolveSessionID` (Go env reader).
- [agents/persona_runtime/session_id.py](../../agents/persona_runtime/session_id.py)
  — `resolve_session_id_and_log` (Python env reader).

**Related Automated Tests**: see "Out of Scope" above.

---

## Preconditions

Same baseline as
[MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions): a
local repo checkout with `make build` already run so `bin/persatrix.exe`
+ a recent orchestrator binary are on disk, and a v0.3.1 (or newer)
build that includes RFC 0031 Phase 1.

This MT **does** require `ANTHROPIC_API_KEY` because the persona-runtime
side of the walkthrough triggers an LLM round-trip to land an episode
row from a chat turn. Operators without an API key can skip Steps 4 and
substitute the equivalent Python-side automated test
(`tests/integration/test_session_id_cross_process.py`) — the storage
contract is identical.

The walkthrough is **destructive** for `data/channels.db` and
`data/memory.db`. Move both aside before starting:

```pwsh
Move-Item data/channels.db data/channels.db.pre-mt-session-001 -ErrorAction SilentlyContinue
Move-Item data/memory.db    data/memory.db.pre-mt-session-001    -ErrorAction SilentlyContinue
```

---

## Test Procedure

### Step 1: Start the stack under `PERSATRIX_SESSION_ID=run-a`

**Action**:

```pwsh
$env:PERSATRIX_SESSION_ID = "run-a"
./bin/orchestrator.exe --env=development 2>&1 | Tee-Object orchestrator-run-a.log
# (in a second shell)
$env:PERSATRIX_SESSION_ID = "run-a"
python -m persatrix_agents.server 2>&1 | Tee-Object persona-run-a.log
```

**Expected**:
- Orchestrator log carries a single line containing
  `PERSATRIX_SESSION_ID` with the value `run-a` (silent on the happy
  path — no INFO/WARN about the env var because the value is
  well-formed; see
  [`TestResolveSessionID_SetReturnsValueQuietly`](../../cmd/orchestrator/session_env_test.go#L40)).
- The persona-runtime log is similarly silent on the env var (the
  happy-path contract is "log when defaulting, silent when set" —
  parity with the Go side).

**Verification**:
- [ ] Neither log contains a WARN/INFO line mentioning
  `PERSATRIX_SESSION_ID` (well-formed values are silent).

---

### Step 2: Create a channel and publish under `run-a`

**Action**:

```pwsh
$body = @'
{"name":"mt-session-001","members":[{"id":"alice","respond":"when_mentioned"}]}
'@
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/v1/channels `
    -Method POST -ContentType 'application/json' -Body $body

Invoke-RestMethod `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-session-001/messages `
    -Method POST -ContentType 'application/json' `
    -Body '{"sender_id":"alice","content":"hello from run-a"}'
```

**Expected**:
- Channel created.
- Message accepted.

**Verification**:
- [ ] Both calls return successful JSON (no HTTP error).

---

### Step 3: Confirm `run-a` rows in `channels.db`

**Action**:

```pwsh
sqlite3 data/channels.db "SELECT id, session_id FROM channels WHERE id='group:mt-session-001';"
sqlite3 data/channels.db "SELECT sender_id, content, session_id FROM messages WHERE channel_id='group:mt-session-001';"
```

**Expected**:
- `channels` row carries `session_id='run-a'`.
- `messages` row carries `session_id='run-a'` and content
  `hello from run-a`.

**Verification**:
- [ ] Both queries print `run-a` in the `session_id` column.

---

### Step 4: Trigger a persona episode under `run-a` (optional, needs API key)

If `ANTHROPIC_API_KEY` is set, drive a chat turn via the CLI so the
persona-runtime writes a `relationships` row + an `episodes` row:

```pwsh
./bin/persatrix.exe chat --agent ember-owl --message "what's on fire?"
```

Otherwise skip to Step 5; the cross-process automated test
(`tests/integration/test_session_id_cross_process.py`) already covers
the Python side equivalent.

**Expected**:
- Persona replies; one new row each in `episodes` and `relationships`
  on `data/memory.db`.

**Verification**:
- [ ] `sqlite3 data/memory.db "SELECT COUNT(*) FROM episodes WHERE session_id='run-a';"`
  is `>= 1`.
- [ ] `sqlite3 data/memory.db "SELECT COUNT(*) FROM relationships WHERE session_id='run-a';"`
  is `>= 1`.

---

### Step 5: Stop both processes; restart under `PERSATRIX_SESSION_ID=run-b`

**Action**:

In each shell:

```pwsh
# Ctrl+C the running process, then:
$env:PERSATRIX_SESSION_ID = "run-b"
./bin/orchestrator.exe --env=development 2>&1 | Tee-Object orchestrator-run-b.log
# (other shell)
$env:PERSATRIX_SESSION_ID = "run-b"
python -m persatrix_agents.server 2>&1 | Tee-Object persona-run-b.log
```

**Expected**:
- Both binaries restart cleanly.
- Neither log mentions `PERSATRIX_SESSION_ID` (happy-path silence).

**Verification**:
- [ ] Both logs are silent about the env var.

---

### Step 6: Publish a second message under `run-b` and confirm row isolation

**Action**:

```pwsh
Invoke-RestMethod `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-session-001/messages `
    -Method POST -ContentType 'application/json' `
    -Body '{"sender_id":"alice","content":"hello from run-b"}'

sqlite3 data/channels.db "SELECT content, session_id FROM messages WHERE channel_id='group:mt-session-001' ORDER BY timestamp;"
```

**Expected**:
- Two `messages` rows on the same channel: the `run-a` write carries
  `session_id='run-a'`, the `run-b` write carries `session_id='run-b'`.
- The channel row itself still carries `session_id='run-a'` (the row
  was *created* under `run-a` and the per-row tag is stable; per-message
  tags are independent).

**Verification**:
- [ ] Output shows both `(hello from run-a, run-a)` and
  `(hello from run-b, run-b)` rows.
- [ ] The `channels.id='group:mt-session-001'` row still shows `run-a`.

---

### Step 7: Confirm per-row first-seen contract on the Python side

If Step 4 ran under `run-a`, repeat it under `run-b` with the *same*
peer participant ID:

```pwsh
./bin/persatrix.exe chat --agent ember-owl --message "different session, same peer"

sqlite3 data/memory.db "SELECT other_participant_id, session_id FROM relationships WHERE participant_id='ember-owl';"
```

**Expected**:
- The `relationships` row for the peer carries `session_id='run-a'`
  (the first-seen value) — Phase 1's per-row contract for
  relationships is "stamp on INSERT, preserve on UPDATE" (mirrors how
  `trust_score` is preserved by `record_interaction`).
- A new `episodes` row exists with `session_id='run-b'`.

**Verification**:
- [ ] Relationships row session_id is `run-a` (unchanged from Step 4).
- [ ] `sqlite3 data/memory.db "SELECT COUNT(*) FROM episodes WHERE session_id='run-b';"`
  is `>= 1`.

---

### Step 8: Verify the unset-env fallback (synthetic `legacy` carve-out)

**Action**:

Stop both processes again, clear the env var, and restart **without**
setting `PERSATRIX_SESSION_ID`:

```pwsh
Remove-Item Env:PERSATRIX_SESSION_ID -ErrorAction SilentlyContinue
./bin/orchestrator.exe --env=development 2>&1 | Tee-Object orchestrator-unset.log
```

Publish one more message:

```pwsh
Invoke-RestMethod `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-session-001/messages `
    -Method POST -ContentType 'application/json' `
    -Body '{"sender_id":"alice","content":"hello from unset"}'

sqlite3 data/channels.db "SELECT content, session_id FROM messages WHERE channel_id='group:mt-session-001' ORDER BY timestamp;"
```

**Expected**:
- Orchestrator log contains a single **INFO** line mentioning
  `PERSATRIX_SESSION_ID`, `unset`, and `legacy` — the documented
  default-fallback log (see
  [`TestResolveSessionID_UnsetDefaultsToLegacy`](../../cmd/orchestrator/session_env_test.go#L21)).
- The new message row carries `session_id='legacy'`.

**Verification**:
- [ ] Orchestrator log contains exactly one INFO line about
  `PERSATRIX_SESSION_ID` mentioning both `unset` and `legacy`.
- [ ] The new message row in `messages` has `session_id='legacy'`.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | Stack starts under `run-a`; logs silent about env var | ☐ |
| 2 | Channel + message created cleanly | ☐ |
| 3 | Both `channels` and `messages` rows tagged `run-a` | ☐ |
| 4 | Persona writes episode + relationship under `run-a` (optional) | ☐ |
| 5 | Restart under `run-b` succeeds | ☐ |
| 6 | New message tagged `run-b`; old rows untouched | ☐ |
| 7 | Relationships row keeps first-seen tag; new episode tagged `run-b` | ☐ |
| 8 | Unset env defaults to `legacy`; INFO log fires | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Non-canonical env value (`PERSATRIX_SESSION_ID="my session"`)

Phase 1 plumbing accepts the value verbatim and emits a WARN log on
**both** the orchestrator and the persona-runtime (parity tests:
[`TestResolveSessionID_InvalidCharsWarnsButAccepts`](../../cmd/orchestrator/session_env_test.go#L59)
on the Go side; `TestNonCanonicalValue` in
[`tests/unit/python/test_session_id_resolve.py`](../../tests/unit/python/test_session_id_resolve.py)
on the Python side). Hard validation lives in Phase 3 CLI's
`persatrix session new`. Both binaries' WARN messages cite the same
regex shape so an operator greps for one phrase across both logs:

```pwsh
$env:PERSATRIX_SESSION_ID = "my session"
./bin/orchestrator.exe --env=development 2>&1 | Select-String "PERSATRIX_SESSION_ID"
# Expect: WARN line mentioning "characters outside [A-Za-z0-9_-]"
python -m persatrix_agents.server 2>&1 | Select-String "PERSATRIX_SESSION_ID"
# Expect: WARN line citing the same canonical regex
```

### Edge Case 2: Phase 2 recall semantics

A repeated read against `episodes` or `relationships` under a different
`PERSATRIX_SESSION_ID` will **still surface the prior session's rows
in Phase 1** — recall has no session filter yet. This is the Phase 2
work item; the dementia-test ([MT-MEMORY-005](MT-MEMORY-005-dementia-test.md))
is the acceptance gate for Phase 2 and will tighten the contract.

### Edge Case 3: Migration replay on existing data

If `data/memory.db` / `data/channels.db` from a pre-RFC-0031 build
exists when v0.3.1 first boots, every existing row picks up
`session_id='legacy'` via the column default. No backfill UPDATE is
required (SQLite ≥3.20 supports `ALTER TABLE ... ADD COLUMN`
with a constant `DEFAULT`). Verify with:

```pwsh
sqlite3 data/memory.db "SELECT DISTINCT session_id FROM episodes WHERE created_at < strftime('%s', '2026-05-13');"
# Expect: legacy
```

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|

---

## Notes

- The Phase 1 contract is **write-path only**. Operators using this MT
  to bake confidence before v0.3.1 release should not infer
  recall-side isolation — that lands later.
- `legacy` is reserved by Phase 3 CLI: `persatrix session new --label legacy`
  is rejected (RFC 0031 OQ #2). The synthetic carve-out has no row in
  the new `sessions` table; it exists only as a string default on
  tagged columns.
- The orchestrator and persona-runtime read the env var independently;
  there is no shared-process coordination. Operators must export the
  same value in both shells to keep a run coherent. Phase 3's CLI
  pointer at `~/.persatrix/active-session` will collapse this into a
  single source of truth.
