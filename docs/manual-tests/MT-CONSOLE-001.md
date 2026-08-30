# Manual Test MT-CONSOLE-001: Web Console — Fresh-Stack Interactions Slice

**Test ID**: `MT-CONSOLE-001`
**Feature Area**: Web Console (RFC 0048 Slice 1 — Interactions)
**Version**: 1.0
**Created**: 2026-06-03
**Last Updated**: 2026-06-03
**Status**: Active

---

## Overview

**Purpose**: Verify the embedded web console (RFC 0048 Phase 1 / Slice 1) end to
end from a fresh build: the orchestrator serves the real Svelte bundle at `/ui`
only with `--enable-ui`, the SPA boots off `/api/v1/ui/config` + `/api/v1/ui/context`,
the Chat panel talks to a persona (with session/epoch scoping), and the
Channel-timeline panel renders history and stays live by polling.

**Scope**: `make ui` build determinism, `--enable-ui` on/off gating, the two
boot endpoints, the Chat panel hero flow (persona pick → send → reply),
session/epoch pass-through, the Channel-timeline polling/publish flow, and the
feature-toggle (`config/ui.yaml`) + security posture.

**Out of Scope**: Memory strip (deferred to Slice 2 — its toggle ships off);
cost panel (Slice 4); authentication (RFC 0039); channel real-time push (OQ4)
and chat token streaming (OQ5). The reload-and-confirm-recall check here reads
recall **from the persona's chat replies**; the memory-strip recall view moves
into this script once Slice 2 lands (per [RFC §Test Strategy](../rfcs/0048-operator-tester-web-console.md#test-strategy)).

---

## Related Documentation

**Feature Documentation**:
- [docs/guides/web-console.md](../guides/web-console.md) — operator/tester guide
- [docs/rfcs/0048-operator-tester-web-console.md](../rfcs/0048-operator-tester-web-console.md) — spec
- [docs/rfcs/0048-phase1-pr-plan.md](../rfcs/0048-phase1-pr-plan.md) — implementation plan
- [internal/server/ui_handlers.go](../../internal/server/ui_handlers.go) — `/api/v1/ui/config` + `/api/v1/ui/context`
- [cmd/orchestrator/ui.go](../../cmd/orchestrator/ui.go) — `--enable-ui` wiring
- [internal/ui/ui.go](../../internal/ui/ui.go) — `embed.FS` asset package
- [config/ui.yaml](../../config/ui.yaml) — feature-toggle surface
- [web/](../../web/) — Svelte SPA source + build

**Related Automated Tests**:
- Go: `internal/server/ui_handlers_test.go`, `cmd/orchestrator/ui_test.go`, `internal/ui/ui_test.go`
- JS: `web/src/App.test.js`, `web/src/panels/Chat.test.js`, `web/src/panels/ChannelTimeline.test.js`

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
- Node 22+: `node --version` (only for the local `make ui` build path; the Docker path builds the bundle in-image)
- `curl` and a browser available

### Application State

- ☐ Clean checkout (the generated UI bundle is gitignored; only the placeholder is tracked)
- ☐ Config files valid: `make validate`
- ☐ At least one persona agent available (e.g. `ember-owl`)
- ☐ `ANTHROPIC_API_KEY` set, **or** use `make demo-offline` (zero-cost mock provider) for the chat steps

### Test Data

No external fixtures required. Requests are constructed inline with `curl`; the
browser steps use the shipped personas/channels.

---

## Test Procedure

### Step 1: Go-only build is green with no JS toolchain

**Action**:

```bash
go build ./...
```

**Expected Result**: Compiles cleanly. The `internal/ui` package builds against
the committed placeholder asset; no Node toolchain is required.

**Verification**:
- [ ] `go build ./...` exits 0 with no JS toolchain present.

---

### Step 2: Build the real bundle and confirm it is served

**Action**:

```bash
make ui                      # web/ → internal/ui/assets/ (Vite, base: /ui/)
make build-orchestrator      # embed the bundle
```

**Expected Result**: `make ui` builds from a fresh `web/` (lockfile committed)
and writes assets into `internal/ui/assets/`, overwriting the placeholder.

**Verification**:
- [ ] `make ui` completes; `internal/ui/assets/index.html` is the real bundle, not the placeholder.

---

### Step 3: Flag-off — `/ui/` is a clean 404

**Action**: Run the orchestrator **without** `--enable-ui` (e.g. `make run`), then:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/ui/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/api/v1/ui/config
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/healthz
```

**Expected Result**: `/ui/` → `404`, `/api/v1/ui/config` → `404`, `/healthz` → `200`.

**Verification**:
- [ ] `/ui/` returns `404` (no `--enable-ui`).
- [ ] `/api/v1/ui/config` returns `404` (no `--enable-ui`).
- [ ] `/healthz` and the existing REST routes are unaffected (`200`).

---

### Step 4: Flag-on — boot endpoints return the expected shape

**Action**: Restart with the console enabled (`make run-ui`, or
`./bin/persatrix-server --enable-ui`), then:

```bash
curl -s http://localhost:8080/api/v1/ui/config | python3 -m json.tool
curl -s http://localhost:8080/api/v1/ui/context | python3 -m json.tool
```

**Expected Result**:

`/api/v1/ui/config` — panels with `enabled` + runtime-derived `available`, plus a build version. Since the [chat-panel-retirement amendment (#516)](../rfcs/0048-operator-tester-web-console.md), chat is folded into a **single consolidated `channel_timeline` panel** (group channels + DMs over one surface) carrying a runtime-derived `create` capability — there is no separate `chat` panel key:

```json
{
  "panels": {
    "channel_timeline": {
      "enabled": true,
      "available": true,
      "create": { "enabled": true, "available": true }
    },
    "memory_strip": { "enabled": false, "available": false },
    "cost": { "enabled": false, "available": true }
  },
  "build": { "version": "..." }
}
```

`/api/v1/ui/context` — the degenerate single-tenant principal:

```json
{ "principal": "local", "tenant": "local", "authenticated": false }
```

**Verification**:
- [ ] `channel_timeline` is `enabled: true` (the consolidated conversation panel); `memory_strip` and `cost` are `enabled: false`.
- [ ] `channel_timeline.available` is `true` when channels are wired (and `false` if you run with channels disabled); `channel_timeline.create` reports the runtime-derived channel-creation capability.
- [ ] `build.version` is a non-empty string (the compiled-in `defaultServiceVersion` when `PERSATRIX_SERVICE_VERSION` is unset).
- [ ] `/api/v1/ui/context` returns `principal: "local"`, `authenticated: false`.

---

### Step 5: Console loads and renders only enabled+available panels

**Action**: Open `http://localhost:8080/ui` in a browser.

**Expected Result**: The console shell loads. The Chat and Channel-timeline
panels render; no slot appears for `memory_strip` or `cost` (disabled).

**Verification**:
- [ ] The console loads at `/ui` (no console errors in the browser dev tools).
- [ ] Chat and Channel-timeline panels are present.
- [ ] No memory-strip or cost panel is shown.
- [ ] No free-text "user" field is presented — identity comes from `/ui/context`.

---

### Step 6: Chat with a persona (hero flow)

**Action**: In the Chat panel, pick a persona (e.g. `ember-owl`), type a
message, and send it.

**Expected Result**: A "thinking…" affordance, then the persona's reply renders.

**Verification**:
- [ ] The persona picker is populated from the live agent list.
- [ ] Sending a message shows a pending state, then the `reply`.
- [ ] The outgoing request carries the `/ui/context`-derived `user_id` and `participant_type:"user"` (inspect in browser network tools).

---

### Step 7: Session / epoch scoping passes through

**Action**: Send a message that the persona will remember (e.g. "My favourite
colour is teal"). Then change the **epoch** selector to a fresh value and ask
"What is my favourite colour?".

**Expected Result**: Under a fresh epoch the persona does **not** recall the
colour (clean-slate isolation). Switching the epoch back (or leaving it at the
original) recalls it.

**Verification**:
- [ ] The session/epoch selector value appears in the outgoing request body.
- [ ] A fresh epoch yields no recall of prior-epoch facts (the v0.3.5 isolation story).
- [ ] Returning to the original epoch/session recalls the fact.

---

### Step 8: Reload and confirm recall in chat replies

**Action**: With the original session/epoch selected, reload the browser and ask
the persona to recall the earlier fact.

**Expected Result**: After a reload, the persona still recalls the fact in its
reply (memory persists across the page reload; recall is read from the chat
reply, not a memory strip).

**Verification**:
- [ ] After reload, the persona recalls the earlier fact in its reply.

---

### Step 9: Channel timeline renders history and polls live

**Action**: In the Channel-timeline panel, pick a group channel. Observe the
history; then publish a message into the channel (via the panel's publish
control, or `curl POST /api/v1/channels/{id}/messages`).

**Expected Result**: History renders newest-first; the published message — and
any agent fan-out replies — appear on a later poll within one interval.

**Verification**:
- [ ] History renders newest-first.
- [ ] A newly published message appears within one poll interval (no manual refresh).
- [ ] Agent replies to a mention fan-out appear on a subsequent poll.

---

### Step 10: Polling pauses when backgrounded

**Action**: With the Channel-timeline panel open, switch to another browser tab
for ~30 s (backgrounding the console tab), then return. Inspect the network log.

**Expected Result**: Polling pauses while the tab is backgrounded and resumes on
return; on an induced error the panel backs off rather than hammering the
endpoint.

**Verification**:
- [ ] Polling stops while the tab is backgrounded (Page Visibility pause).
- [ ] Polling resumes when the tab is foregrounded.
- [ ] Head-poll de-dupes by message id (the full history is not re-fetched each tick).

---

### Step 11: Feature-toggle validation rejects an authored `available:`

**Action**: Temporarily add an `available: true` key under `panels.chat` in
[`config/ui.yaml`](../../config/ui.yaml), then:

```bash
make validate
```

Revert the edit afterward.

**Expected Result**: `make validate` **fails** — `available` is runtime-derived
and the schema forbids it (`additionalProperties:false`).

**Verification**:
- [ ] `make validate` rejects an `available:` key in `config/ui.yaml`.
- [ ] Removing the key restores a green `make validate`.

---

### Step 12: Docker demo path serves the real console

**Action**:

```bash
make demo-offline            # or: docker compose up --build
# open http://localhost:8080/ui
```

**Expected Result**: From a clean clone with no host JS toolchain, the image
build bakes the real Svelte bundle and the console loads at `/ui`.

**Verification**:
- [ ] `docker compose build` (via `demo-*`) embeds the real console with no host `make ui`.
- [ ] The console loads and the Chat panel works against the demo personas.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `go build ./...` green with no JS toolchain | ☐ |
| 2 | `make ui` builds the real bundle into `internal/ui/assets/` | ☐ |
| 3 | Flag-off: `/ui/` + `/api/v1/ui/*` are 404; existing routes unaffected | ☐ |
| 4 | Flag-on: config/context endpoints return the expected shape | ☐ |
| 5 | Console renders only enabled+available panels; no free-text user | ☐ |
| 6 | Chat hero flow: pick persona → send → reply | ☐ |
| 7 | Session/epoch selectors scope correctly (fresh epoch = clean slate) | ☐ |
| 8 | Recall persists across a reload (read from chat replies) | ☐ |
| 9 | Channel timeline renders history + polls live; publish appears | ☐ |
| 10 | Polling pauses backgrounded, backs off on error, head-poll de-dupes | ☐ |
| 11 | `make validate` rejects an authored `available:` key | ☐ |
| 12 | Docker demo serves the real console with no host JS toolchain | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Over-length chat message

**Scenario**: Send a chat message exceeding 4 000 characters from the Chat panel.

**Expected**: The panel surfaces the server's `BAD_REQUEST` envelope (or a
client-side length guard) without crashing.

### Edge Case 2: Channels disabled

**Scenario**: Run `--enable-ui` with channels not configured.

**Expected**: `/api/v1/ui/config` reports `channel_timeline.available:false`; the
console hides the Channel-timeline slot; the Chat panel still works.

### Edge Case 3: Absent `config/ui.yaml`

**Scenario**: Remove `config/ui.yaml` and run with `--enable-ui`.

**Expected**: Slice-1 defaults apply (chat + channel_timeline on, others off);
the console boots normally (zero-config case).

### Edge Case 4: Flag-on binary built without `make ui`

**Scenario**: `make build-orchestrator` without `make ui`, then `--enable-ui`.

**Expected**: `/ui/` serves the placeholder ("run `make ui`") rather than
failing the build — the Go-only-build guarantee.

---

## Security Posture (verify, do not skip)

| Check | Expected |
|-------|----------|
| Default bind | The orchestrator binds `127.0.0.1` by default (`--http-bind`). |
| Default flag | `--enable-ui` is **off** by default. |
| Exposure rule | Under the default `auth.mode: disabled`, exposing the console beyond localhost requires an authenticating reverse proxy; since v0.3.12, `auth.mode: enabled` over HTTPS is the first-party alternative ([MT-AUTH-001](MT-AUTH-001.md)) — documented in [web-console.md §Security](../guides/web-console.md#security--exposure-beyond-localhost). |
| Writes | Slice 1's only writes are chat + optional channel publish; no admin/control-plane actions exist (Slice 5, gated on RFC 0039). |
