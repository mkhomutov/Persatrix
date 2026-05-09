# Manual Test MT-CHANNEL-001: `persatrix channel list` / `join` against a docker-composed orchestrator

**Test ID**: `MT-CHANNEL-001`
**Feature Area**: Channels (CLI)
**Version**: 1.0
**Created**: 2026-05-09
**Last Updated**: 2026-05-09
**Status**: Active

---

## Overview

**Purpose**: Verify the discovery + membership half of the Rust CLI channel surface (RFC 0011 §F)
end-to-end against a live orchestrator: `persatrix channel list` (human + JSON) and
`persatrix channel join` (bare name, fully-qualified id, JSON output, `--respond` policy
selection, client-side input validation).

**Scope**: `channel list`, `channel join` subcommands and their clap surface
(`--server`, `--as`, `--respond`, `--json`).

**Out of Scope**: `channel send`, `channel reply`, `channel history` (covered by
[MT-CHANNEL-002](MT-CHANNEL-002.md)). `channel watch` (covered by
[MT-CHANNEL-003](MT-CHANNEL-003.md)). Group-channel **creation** is server-side only —
no `channel create` CLI subcommand exists in v0.3.0; the test does the create via REST.

---

## Related Documentation

- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) §C, §F
- [docs/rfcs/0011-pr-plan.md](../rfcs/0011-pr-plan.md) — PR 6 row
- [cli/src/commands/channel.rs](../../cli/src/commands/channel.rs)
- [cli/src/commands/channel_dispatch.rs](../../cli/src/commands/channel_dispatch.rs)
- [internal/server/channel_handlers.go](../../internal/server/channel_handlers.go)

**Related Automated Tests**:
- `cli/src/commands/channel_tests.rs` — pure helpers (canonicalize, mention expansion, watch state)
- `cli/src/commands/channel_types.rs` — serde-contract tests
- `internal/server/channel_handlers_test.go`, `channel_handlers_pagination_test.go`

---

## Preconditions

### System Requirements

- Windows 10/11 (x64), macOS 12+, or Linux (Ubuntu 22.04+)
- Docker Desktop ≥ 4.x or Docker Engine ≥ 24 with Compose v2 (`docker compose version`)
- Rust stable (`cargo --version`) for building the CLI

### Application State

- ☐ `.env` exists at repo root and contains `ANTHROPIC_API_KEY=…` (the agent containers in
  `docker-compose.yaml` fail-fast otherwise; the channel REST surface itself does not need
  the key, but the compose stack does)
- ☐ All local ports free: 8080, 9090, 50051–50054, 4317/4318, 16686, 3100, 9091
- ☐ CLI built: `make build-cli` → `bin/persatrix(.exe)`
- ☐ Compose stack up and healthy: `docker compose up -d --build` then
  `docker compose ps` shows every service `Up … (healthy)`

### Stack-up procedure (shared with MT-CHANNEL-002 / MT-CHANNEL-003)

```pwsh
make build-cli
docker compose up -d --build
docker compose ps   # wait until orchestrator + four agents report (healthy)

(Invoke-WebRequest http://127.0.0.1:8080/healthz -UseBasicParsing).Content
# expect: {"status":"ok"}
```

> **Port-collision recovery (Windows / pwsh)** — if a previous session left a
> `persatrix-server.exe` on port 8080:
>
> ```pwsh
> Get-Process persatrix-server -ErrorAction SilentlyContinue | Stop-Process -Force
> Get-NetTCPConnection -LocalPort 8080,9090 -ErrorAction SilentlyContinue |
>   ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
> ```

---

## Test Procedure

### Step 1: List channels (empty-state and JSON shape)

**Action**:

```pwsh
./bin/persatrix.exe channel list
./bin/persatrix.exe channel list --json
```

**Expected**:
- Exit code 0 in both invocations.
- Empty volume: human prints `No channels.`; JSON prints `[]`.
- Populated volume: human prints one row per channel
  (`<id>  <type>  <created_at>` — `id` cyan, timestamp dim); JSON prints a
  single-line array of `ChannelView` objects.

**Verification**:
- [ ] Both commands exit 0.
- [ ] Human row format matches `<id>  <type>  <iso8601>`.
- [ ] JSON output is a single line and parses (`ConvertFrom-Json` succeeds).

---

### Step 2: Create a group channel via REST (no `channel create` CLI in v0.3.0)

**Action**:

```pwsh
$body = '{"name":"mt-channel-001","members":[{"id":"alice","respond":"when_mentioned"}]}'
(Invoke-WebRequest -Uri http://127.0.0.1:8080/api/v1/channels `
    -Method POST -ContentType 'application/json' -Body $body -UseBasicParsing).Content
```

**Expected**:
- HTTP 201 (or 200) with body
  `{"id":"group:mt-channel-001","name":"mt-channel-001","channel_type":"group", …, "created_at":…}`.
- Channel id is the canonical `group:<name>` form (`channel_handlers.go` derives it from `name`).

**Verification**:
- [ ] Response body contains `"id":"group:mt-channel-001"` and `"channel_type":"group"`.

---

### Step 3: Join — three flag combinations

**Action**:

```pwsh
# 3a — bare name (canonicalize_channel_id maps to group:<name>)
./bin/persatrix.exe channel join mt-channel-001 --as bob

# 3b — fully-qualified id passes through unchanged
./bin/persatrix.exe channel join group:mt-channel-001 --as carol --respond always

# 3c — JSON shape + non-default respond policy
./bin/persatrix.exe channel join mt-channel-001 --as dave --respond never --json
```

**Expected**:
- 3a, 3b: human output `Joined #group:mt-channel-001 as <user>` (channel id cyan, user bold).
- 3c: single-line JSON `{"channel_id":"group:mt-channel-001","respond":"never","user_id":"dave"}`.
- All three exit 0.

**Verification**:
- [ ] 3a + 3b print the expected human form.
- [ ] 3c is single-line JSON and `ConvertFrom-Json` succeeds.

---

### Step 4: Confirm membership state via `GET /channels/{id}`

**Action**:

```pwsh
./bin/persatrix.exe channel list | Select-String "group:mt-channel-001"
(Invoke-WebRequest http://127.0.0.1:8080/api/v1/channels/group:mt-channel-001 `
  -UseBasicParsing).Content
```

**Expected**:
- `channel list` shows the new channel row.
- `GET /channels/{id}` body's `members` array contains all four ids (`alice` from create,
  `bob`/`carol`/`dave` from joins) with the matching `respond` policy
  (`when_mentioned` / `when_mentioned` / `always` / `never`).

**Verification**:
- [ ] All four members present with correct `respond` policy.
- [ ] Each member entry has a non-empty `joined_at` timestamp.

---

### Step 5: Edge cases — client-side rejection before any network round-trip

**Action**:

```pwsh
# 5a — invalid --respond value (clap value-enum rejection)
./bin/persatrix.exe channel join mt-channel-001 --as bob --respond yelling

# 5b — invalid --as (validate_resource_id: ^[a-z0-9][a-z0-9-]*[a-z0-9]$)
./bin/persatrix.exe channel join mt-channel-001 --as 'Bad ID!'

# 5c — missing positional <NAME>
./bin/persatrix.exe channel join
```

**Expected**:
- 5a: clap error
  `error: invalid value 'yelling' for '--respond <RESPOND>'  [possible values: when_mentioned, always, never]`,
  exit 2.
- 5b: `error: invalid user id "Bad ID!": must start with lowercase letter or digit`, exit 1.
- 5c: clap usage error `error: the following required arguments were not provided: <NAME>`, exit 2.

**Verification**:
- [ ] All three fail before opening a TCP connection (no `connection failed` text, no panic).
- [ ] Exit codes are non-zero (clap → 2, app validation → 1).

---

### Step 6: List pagination warning (cold-path note)

`channel list` warns on stderr when the server returns a non-empty `next_cursor` (PR #302
finding 1). The CLI does not expose `--limit` / `--cursor` flags for `list`, and the
server's default page size `channelDefaultListLimit = channels.DefaultMaxChannels` (50)
matches the global cap, so this branch is **cold** in normal use. The behaviour is covered
by `internal/server/channel_handlers_pagination_test.go` (server side) and the warning
literal `FULL_PAGE_WARNING_TEXT` lives in [`channel.rs`](../../cli/src/commands/channel.rs).

To reproduce the warning manually you must hit REST directly:

```pwsh
(Invoke-WebRequest "http://127.0.0.1:8080/api/v1/channels?limit=1" -UseBasicParsing).Content
# `next_cursor` will be populated when ≥ 2 channels exist.
```

**Verification** (optional):
- [ ] REST call returns a body with non-empty `next_cursor` when ≥ 2 channels exist.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `channel list` (human + JSON) returns 0 with the expected shape | ☐ |
| 2 | Group channel created via REST | ☐ |
| 3 | Three join variations all succeed (bare, FQ, JSON) | ☐ |
| 4 | `GET /channels/{id}` reports all four members with correct respond policy | ☐ |
| 5 | Three edge cases reject client-side, no panic | ☐ |
| 6 | Pagination warning literal verified by server-side test (REST reproduction optional) | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Orchestrator not reachable

`./bin/persatrix.exe channel list --server http://127.0.0.1:9` should print a
clean `error: connection failed: …` to stderr and exit non-zero — never panic with a
Rust `unwrap` backtrace.

### Edge Case 2: Stale channels in the SQLite volume

The orchestrator persists channels at `/var/lib/persatrix/channels.db` on the
`orchestrator-data` named volume. Earlier runs (e.g. chat-as-DM creates from MT-CHAT-001)
will appear in `channel list` before this test starts. That is **fine** — the test
validates relative state (one freshly-created `group:mt-channel-001` shows up alongside
whatever was there). To start clean, tear down with `-v`:

```pwsh
docker compose down -v
docker compose up -d --build
```

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-09 | Claude (PR #302 prep) | Windows 11 | Pass | Full live run on `feature/v030-rfc0011-cli-subcommands` @ `82a0602`. Compose stack healthy (orchestrator + 4 agents). Step 1 — empty state showed pre-existing DM channels from earlier sessions; both human + JSON exit 0 with expected shape. Step 2 — `group:mt-channel-001` created with HTTP 200 body containing `"channel_type":"group"`. Step 3 — three join variants all succeed; 3a/3b human output matched `Joined #group:mt-channel-001 as <user>`; 3c JSON `{"channel_id":"group:mt-channel-001","respond":"never","user_id":"dave"}`. Step 4 — `GET /channels/group:mt-channel-001` returned four members with respond policies `when_mentioned`/`when_mentioned`/`always`/`never`. Step 5 — all three rejections produced the documented stderr; no TCP connection attempted. Step 6 — REST reproduction of pagination warning skipped (cold path; covered by server-side test). |

---

## Notes

- The CLI binary is built per `make build-cli` (`cargo build --release` in `cli/`) and
  lands at `bin/persatrix(.exe)`.
- The `--respond` value-parser is a Rust `ValueEnum` (`channel_dispatch.rs`) so typos
  fail at clap parse time, before any HTTP round-trip — see `cmd_channel_join` for the
  wire-form mapping in [`channel_dispatch.rs`](../../cli/src/commands/channel_dispatch.rs).
- The `validate_resource_id` shape is shared with `cmd_chat` (PR #302 finding 3) — every
  configured agent id matches the stricter pattern, so client-side rejection beats a
  generic `400` from the unauthenticated REST surface.
- Channels are unauthenticated in v0.3.0 (RFC 0011 §C trust boundary); auth is deferred
  to RFC 0009.
