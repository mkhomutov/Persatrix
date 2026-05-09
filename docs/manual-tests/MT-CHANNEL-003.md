# Manual Test MT-CHANNEL-003: `persatrix channel watch` polling, dedup, and full-page warning

**Test ID**: `MT-CHANNEL-003`
**Feature Area**: Channels (CLI)
**Version**: 1.0
**Created**: 2026-05-09
**Last Updated**: 2026-05-09
**Status**: Active

---

## Overview

**Purpose**: Verify the long-running `persatrix channel watch` subcommand against a live
orchestrator: poll cadence (`--interval`), per-message-id high-watermark dedup
(`WatchState`), backlog→chronological reordering, JSON Lines output, the
first-poll-suppressed full-page warning (PR #302 finding 5), and clean failure
on an unknown channel.

**Scope**: `channel watch` and the dedup ring helpers in
[`channel.rs`](../../cli/src/commands/channel.rs):
`WatchState::with_cap`, `WatchState::apply_batch`, `watch_seen_cap_for`,
`FULL_PAGE_WARNING_TEXT`, `DEFAULT_WATCH_INTERVAL_SECS`.

**Out of Scope**: `list`, `join` (covered by [MT-CHANNEL-001](MT-CHANNEL-001.md)).
`send`, `reply`, `history` (covered by [MT-CHANNEL-002](MT-CHANNEL-002.md)). The
ring-eviction-after-cap edge case is covered by `channel_tests.rs::watch_state_*`
unit tests; this MT validates the steady-state user-visible behaviour at the default
ring cap.

---

## Related Documentation

- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) §F (output formats),
  RFC 0011 OQ #4 (poll cadence)
- [cli/src/commands/channel.rs](../../cli/src/commands/channel.rs) (`cmd_channel_watch`,
  `WatchState`, `watch_seen_cap_for`)

**Related Automated Tests**:
- `cli/src/commands/channel_tests.rs` —
  `watch_state_dedups`, `watch_state_evicts_oldest_on_overflow`,
  `apply_batch_reverses_to_chronological`, `watch_seen_cap_for_*`

---

## Preconditions

Same as [MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions). This test
**depends on MT-CHANNEL-001 + MT-CHANNEL-002** having created and populated
`group:mt-channel-001`. Run them in order, or recreate the channel and post a few
messages first.

> The watch test starts a long-running CLI process and intentionally interleaves
> `send` invocations from the **same** machine. On Windows / pwsh, the easiest
> pattern is `Start-Process … -RedirectStandardOutput <file>` so the watch process
> runs in the background while the test driver issues sends — that is the
> approach used in the result row below.

---

## Test Procedure

### Step 1: Backlog → chronological reordering, live message visibility, dedup

The server returns messages newest-first; `WatchState::apply_batch` reverses each
batch to chronological so the watcher prints in the order humans expect, and only
emits ids it has not yet seen.

**Action** (PowerShell):

```pwsh
$logFile = "$env:TEMP\persatrix-watch-out.log"
Remove-Item $logFile -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath "$PWD\bin\persatrix.exe" `
    -ArgumentList @("channel","watch","mt-channel-001","--interval","2","--limit","5") `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError "$env:TEMP\persatrix-watch-err.log" `
    -PassThru -NoNewWindow

Start-Sleep -Seconds 3
"after first poll: $((Get-Content $logFile | Measure-Object -Line).Lines) lines"

./bin/persatrix.exe channel send mt-channel-001 "live during watch A" --as alice
Start-Sleep -Seconds 3
"after second poll: $((Get-Content $logFile | Measure-Object -Line).Lines) lines"

./bin/persatrix.exe channel send mt-channel-001 "live during watch B" --as bob
Start-Sleep -Seconds 3
"after third poll: $((Get-Content $logFile | Measure-Object -Line).Lines) lines"

Stop-Process -Id $proc.Id -Force
"--- full stdout ---"
Get-Content $logFile
"--- stderr ---"
Get-Content "$env:TEMP\persatrix-watch-err.log"
```

**Expected**:
- stderr line on startup: `Watching #group:mt-channel-001 (poll every 2s; Ctrl-C to stop)`.
- After first poll: 5 lines on stdout — the latest 5 messages from MT-CHANNEL-002,
  printed **chronological** (oldest first) even though the server returned newest-first.
- After second poll: line count grows by exactly 1 (only `live during watch A` is new;
  the four overlap-ids are dedup-suppressed by `WatchState`).
- After third poll: line count grows by exactly 1 (only `live during watch B`).
- No duplicates anywhere in stdout.

**Verification**:
- [ ] First-poll output is chronological (timestamps strictly non-decreasing).
- [ ] Second + third polls each add exactly 1 line to stdout.
- [ ] No message id appears twice.

---

### Step 2: `--json` produces JSON Lines (one object per line)

**Action**:

```pwsh
$logFile = "$env:TEMP\persatrix-watch-json.log"
Remove-Item $logFile -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath "$PWD\bin\persatrix.exe" `
    -ArgumentList @("channel","watch","mt-channel-001","--interval","2","--limit","2","--json") `
    -RedirectStandardOutput $logFile -PassThru -NoNewWindow

Start-Sleep -Seconds 3
./bin/persatrix.exe channel send mt-channel-001 "json-mode A" --as alice | Out-Null
Start-Sleep -Seconds 3
Stop-Process -Id $proc.Id -Force

Get-Content $logFile | ForEach-Object {
    try { $obj = $_ | ConvertFrom-Json; "OK id=$($obj.id) sender=$($obj.sender_id)" }
    catch { "FAIL: $_" }
}
```

**Expected**:
- Every stdout line parses individually as a `ChannelMessage`.
- Output is **NDJSON / JSON Lines** — *not* a single JSON array. (`history --json`
  emits an array because it is a one-shot read; `watch --json` emits one object
  per new message because it is a continuous stream.)

**Verification**:
- [ ] Every line passes `ConvertFrom-Json`.
- [ ] No line begins with `[` or ends with `]` (would indicate accidental array wrapping).

---

### Step 3: Full-page warning fires only after the first poll (PR #302 finding 5)

The watcher warns on stderr when an entire `--limit`-sized page is unseen,
because that means messages may have rolled past the polling window between
ticks. The warning is **suppressed on the first poll**: poll #1 always returns
the latest page and "all unseen" there just means the channel had ≥ `--limit`
prior messages, not that anything was lost.

**Action**:

```pwsh
$logOut = "$env:TEMP\persatrix-watch-fp.log"
$logErr = "$env:TEMP\persatrix-watch-fp-err.log"
Remove-Item $logOut, $logErr -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath "$PWD\bin\persatrix.exe" `
    -ArgumentList @("channel","watch","mt-channel-001","--interval","4","--limit","2") `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -PassThru -NoNewWindow

# wait for first poll (which must NOT warn even though it is full)
Start-Sleep -Seconds 5

# burst more new messages than --limit between polls so the page rolls over
./bin/persatrix.exe channel send mt-channel-001 "burst 1" --as alice | Out-Null
./bin/persatrix.exe channel send mt-channel-001 "burst 2" --as alice | Out-Null
./bin/persatrix.exe channel send mt-channel-001 "burst 3" --as alice | Out-Null
Start-Sleep -Seconds 5

Stop-Process -Id $proc.Id -Force
"--- stderr (warning expected, NOT on first poll) ---"
Get-Content $logErr
```

**Expected** stderr:

```
Watching #group:mt-channel-001 (poll every 4s; Ctrl-C to stop)
warning: polled page was completely full of new messages — the page may have rolled over; consider raising --limit or lowering --interval
```

The warning literal is `FULL_PAGE_WARNING_TEXT` in
[`channel.rs`](../../cli/src/commands/channel.rs).

**Verification**:
- [ ] Exactly one `Watching …` line and exactly one `warning: polled page …` line on stderr.
- [ ] The warning **does not** appear before the burst (i.e. not on poll #1).

---

### Step 4: Unknown channel — clean failure, not a hang

`channel watch` performs the first poll synchronously and returns the server's
404 verbatim via `api_error_message`. There is no infinite-retry loop on
permanent errors.

**Action**:

```pwsh
./bin/persatrix.exe channel watch no-such-channel --interval 1
```

**Expected**:
- stderr `Watching #group:no-such-channel (poll every 1s; Ctrl-C to stop)` followed by
  `error: 404 Not Found: channel not found`.
- Exit 1 (the process terminates, does not loop).

**Verification**:
- [ ] Process exits with non-zero status within ~1 second.
- [ ] No Rust panic backtrace.

---

### Step 5: `--limit` scales the dedup ring (`watch_seen_cap_for` — note only)

`watch_seen_cap_for(limit)` clamps the `WatchState` ring to
`[WATCH_SEEN_CAP, WATCH_SEEN_CAP_CEILING] = [1024, 16384]`, scaling at
4× `--limit`. The default 50-page → 1024 ring (floor); a `--limit 1000`
(server max) → 4000 ring; `--limit 100000` (a typo) → clamped to 16384, not
allocated as 400000.

This is internal scaling; the user-visible effect is "back-to-back full pages
never re-emit at default settings" — already covered transitively by Step 1's
overlap-window dedup. The clamp is exhaustively covered by
`channel_tests.rs::watch_seen_cap_for_*`.

**Verification** (optional):
- [ ] No live reproduction needed; recorded here as a covered cold-path branch.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Backlog chronological + live messages + dedup all hold | ☐ |
| 2 | `--json` produces JSON Lines (one object per line) | ☐ |
| 3 | Full-page warning fires after first poll, not before | ☐ |
| 4 | Unknown channel exits 1 with clean error | ☐ |
| 5 | Ring-cap clamp covered by unit test (no live repro needed) | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Orchestrator restart during a watch

The watcher does **not** auto-reconnect on transient stream loss in v0.3.0
(unlike `persatrix logs --follow`, which streams over SSE — see MT-LOGS-001
Step 3). A `connection failed` returns from the next poll attempt and the
process exits non-zero. This is by design — `channel watch` is a thin polling
loop, not a stream — but is worth noting for operators who expect SSE-like
resilience.

### Edge Case 2: Channel with zero messages

Watch attaches, prints `Watching #… (poll every Ns; Ctrl-C to stop)` on stderr,
and stays silent until messages arrive. No "no messages" line is printed
(unlike `history`, which prints `No messages.`).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-05-09 | Claude (PR #302 prep) | Windows 11 | Pass | Full live run on `feature/v030-rfc0011-cli-subcommands` @ `82a0602` against the docker-composed stack. Step 1 — first poll printed 5 chronological backlog rows (`alice: ping bob` → `alice: everyone except me` → `carol: json shape check` → `bob: threaded reply` → `alice: self-mention test`); poll #2 added exactly `alice: live during watch A`; poll #3 added exactly `bob: live during watch B`; total 7 lines, zero duplicates. Step 2 — three NDJSON lines, every one parsed via `ConvertFrom-Json`. Step 3 — stderr matched exactly: one `Watching …` line on startup followed by one `warning: polled page was completely full …` line after the burst; warning was NOT printed for the first poll despite the channel having ≥ 2 prior messages. Step 4 — exited 1 within ~1 s with `error: 404 Not Found: channel not found`. Step 5 — not exercised live (covered by `watch_seen_cap_for_*` unit tests in `channel_tests.rs`). |

---

## Notes

- `Stop-Process -Force` is used in the steps above only to terminate the watcher
  cleanly from a non-interactive script. Operators normally stop `channel watch`
  via Ctrl-C; the CLI does not install a custom SIGINT handler — Tokio's default
  cancellation tears down the polling loop.
- `--interval` floors at 1 second (`interval_secs.max(1)` in `cmd_channel_watch`)
  so `--interval 0` does not spin a tight loop.
- Default values: `--interval 5` (RFC 0011 OQ #4), `--limit 50`. Both surfaced
  as `DEFAULT_WATCH_INTERVAL_SECS` / `DEFAULT_HISTORY_LIMIT` in
  [`channel.rs`](../../cli/src/commands/channel.rs).
