# RFC 0048 — PR Implementation Plan (Phase 1 — Slice 1, v0.3.6 scope)

**RFC**: [0048-operator-tester-web-console.md](0048-operator-tester-web-console.md)
**Created**: 2026-06-02
**Branch prefix**: `feature/v036-rfc0048p1-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md) (< 500 lines of meaningful change per PR)
**Master plan**: none yet — no `v0.3.6-plan.md` exists at plan-authoring time. This plan stands alone; if a v0.3.6 master plan is later authored it should reference this workstream rather than the reverse.

---

## Overview

RFC 0048 ships an embedded, same-origin **web console** for operators and testers, delivered as feature-toggled vertical slices. **Phase 1 = Slice 1 (Interactions)**: a newcomer opens `http://localhost:8080/ui`, picks a persona, chats with it, and watches a channel — with zero CLI knowledge. The backend is mostly a *render-over-existing-API* problem: chat, channel list/history/publish, and agent list/info already exist per RFC 0002 / RFC 0011 / RFC 0016. What is genuinely new in Phase 1 is (a) the embedded-asset scaffold (`WithUI` `ServerOption` + `--enable-ui` + `embed.FS`), (b) two small read-only endpoints (`/api/v1/ui/config`, `/api/v1/ui/context`), and (c) a Svelte SPA plus the JS build step that produces the embedded assets.

Phase 1 splits into **6 PRs**. PR 1 lands the Go static-serving scaffold with a committed placeholder asset (so `go build` works with **zero** JS toolchain). PR 2 adds the feature-toggle / forward-compat endpoints plus `config/ui.yaml` and its schema. PR 3 introduces the JS toolchain and the Svelte shell that boots off those endpoints. PRs 4 and 5 add the Chat and Channel-timeline panels. PR 6 is docs + closeout. The split keeps every Go-only change reviewable without a JS reviewer, isolates the two repo-firsts (`go:embed` and a JS build) into named PRs, and lets the SPA shell merge before either interactive panel.

**Memory strip is out of scope** — deferred to Slice 2 by RFC decision (2026-06-02). Its `memory_strip` toggle ships defaulting **off** (PR 2), so the panel lands additively in Slice 2 with no rework to Slice 1. See [RFC §D.3](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero) and [RFC §G](0048-operator-tester-web-console.md#g-api-gaps--required-backend-work).

**Prerequisite**: RFC 0002 (REST API), RFC 0011 (Channels), RFC 0016 (Chat) all implemented (✅). **No dependency on RFC 0039** (auth) for Phase 1 — Slice 1 is interact-only against existing endpoints. The console treats today's no-auth localhost mode as the degenerate single-tenant case (`principal=local`) per [RFC §F](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility).

### Open-question resolutions (locked in the RFC, 2026-06-02)

All five RFC open questions are resolved in [RFC §Open Questions](0048-operator-tester-web-console.md#open-questions); this plan consumes them, it does not re-litigate them.

- **OQ1 — framework: Svelte.** Plain Svelte + Vite emitting static files. The `WithUI(fs.FS)` contract is framework-agnostic, so the choice does not leak past PR 3.
- **OQ2 — toggle surface: `config/ui.yaml`.** Consistent with `channels.yaml` / `bridges.yaml` / `agents.yaml` and the `config/environments/*` override mechanism; absent-file → defaults (Slice 1 panels on, everything else off).
- **OQ3 — read-only memory endpoint / memory strip: deferred to Slice 2.** Slice 1 ships chat + channel timeline only. The `memory_strip` toggle ships off.
- **OQ4 — channel real-time: deferred.** Slice 1 polls the existing messages endpoint; a channel SSE/WebSocket is a named later enhancement.
- **OQ5 — chat token streaming: deferred.** Slice 1 uses synchronous request/response chat.

### Plan-authoring decisions (resolve RFC implementation ambiguities)

These are decisions this plan makes that the RFC left to implementation:

- **D1 — Client-side routing: hash-mode.** The RFC notes a bare `http.FileServer` over `embed.FS` returns 404 for history-mode client routes unless `/ui/` falls back to `index.html`, and that "hash-mode routing sidesteps this." Slice 1 uses **hash-mode** (`/ui/#/chat`, `/ui/#/channels`) so PR 1's static handler stays a plain `http.FileServer` with no SPA-fallback shim. Revisit if a later slice wants clean history-mode URLs.
- **D2 — `go:embed` placeholder.** `go:embed` fails to compile on an empty directory, and the JS build does not run on a Go-only `go build ./...` (or in the Go-only CI lane). PR 1 commits a minimal placeholder `index.html` into the embed package's asset directory so the package always compiles; PR 3's build overwrites it with the real bundle. The build step writes into the **same** embedded directory, so the committed placeholder and the generated bundle never coexist in a release artifact.
- **D3 — Middleware placement.** `/api/v1/ui/config` and `/api/v1/ui/context` are API endpoints and ride the **existing** rate-limit / audit / metrics middleware stack (they are registered on `s.mux` like every other `/api/v1/*` route, which the middleware wraps — see [internal/server/server.go](../../internal/server/server.go) `apiH`). Static `/ui/` asset serving is registered on the same mux but is read-only embedded content; if asset fetches trip the rate limiter in practice, exempting the `/ui/` prefix is a follow-up, flagged in [§Risk](#risk-and-mitigations) rather than pre-optimised.
- **D4 — `validate.py` wiring.** `config/ui.yaml` is only schema-checked if `agents/validate.py`'s `_SCHEMA_MAP` gains a `"ui.yaml": "ui.schema.json"` entry — the RFC's "Files Touched" table omits this Python edit. PR 2 adds both the schema file **and** the map entry, mirroring the existing `channels.yaml` → `channel.schema.json` row.

---

## Dependency Graph

```
PR 1 (WithUI ServerOption + embed package w/ placeholder + --enable-ui + static /ui/ serving)
  ↓
PR 2 (/api/v1/ui/config + /api/v1/ui/context handlers + config/ui.yaml + schema + validate.py map)
  ↓
PR 3 (JS toolchain + Svelte shell + Makefile/CI build step; boots off config/context, renders panel slots)
  ↓
  ├─ PR 4 (Chat panel)              ──┐
  └─ PR 5 (Channel timeline panel)  ──┤  (independent; PR 5 may open before PR 4 merges)
                                       ↓
PR 6 (Docs + CHANGELOG + ROADMAP + RFC/PR-plan status closeout)
```

PRs 1–2 are Go + config + schema only — fully testable with no JS toolchain, and `--enable-ui` defaults off so neither changes default runtime behaviour. PR 3 is the first PR that adds JavaScript and the build step. PRs 4 and 5 each add one panel over today's API and are mutually independent (both depend only on PR 3's shell); the plan sequences PR 4 → PR 5 for review tidiness but they may run in parallel. PR 6 closes out once both panels merge.

---

## PR Sequence

### PR 1: `feature/v036-rfc0048p1-withui-scaffold` — `WithUI` ServerOption + Embed Package + Static Serving

**Depends on**: Nothing (v0.3.5 baseline).
**Purpose**: Stand up the same-origin static-asset scaffold following the orchestrator's optional-subsystem pattern (the same functional-`ServerOption` + nil-safe gating used by channels, cost, and the log buffer — see [cmd/orchestrator/channels.go](../../cmd/orchestrator/channels.go) and [internal/server/server.go](../../internal/server/server.go)). No JS in this PR: a committed placeholder asset proves the wiring end-to-end and keeps `go build ./...` green for Go-only contributors and the Go CI lane.

#### Scope

| File | Change |
|------|--------|
| `internal/ui/ui.go` (new) | Embed package. `//go:embed all:assets` over an `assets/` subdir; exposes `func Assets() fs.FS` returning the `assets` sub-tree (`fs.Sub`). `all:` so dot-prefixed build outputs (e.g. Vite's `.vite/`) are not silently dropped. `__all__`-equivalent: only `Assets()` is exported. |
| `internal/ui/assets/index.html` (new) | **Committed placeholder** (per [D2](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities)). Minimal HTML reading "Persatrix console — assets not built; run `make ui`." So `go:embed` compiles and a flag-on binary built without the JS step serves a clear message instead of failing the build. PR 3's `make ui` overwrites this path. |
| `internal/ui/assets/.gitignore` (new) | Ignore everything except `index.html` (`*` + `!index.html` + `!.gitignore`) so PR 3's generated bundle is **not** committed — the bundle is a build artifact, only the placeholder is tracked. |
| [`internal/server/server.go`](../../internal/server/server.go) | Add `uiFS fs.FS` field; add `func WithUI(uiFS fs.FS) ServerOption` beside the existing `With*` options (~line 192). In `registerRoutes`, only when `s.uiFS != nil`, register `s.mux.Handle("GET /ui/", http.StripPrefix("/ui/", http.FileServer(http.FS(s.uiFS))))`. (The config/context handlers land in PR 2.) `GET /ui` without trailing slash is redirected to `/ui/` by `net/http`'s subtree pattern — no extra route. |
| [`cmd/orchestrator/main.go`](../../cmd/orchestrator/main.go) | Add `enableUI = flag.Bool("enable-ui", false, "Serve the embedded web console at /ui (RFC 0048; localhost-only until RFC 0039 auth)")`. When set, append `server.WithUI(ui.Assets())` to `srvOpts` (~line 246, beside the other conditional options). Defaults **off** per [RFC §Security](0048-operator-tester-web-console.md#security-considerations). |
| `internal/server/ui_handlers_test.go` (new) | Integration: with `WithUI(fstest.MapFS{...})`, `GET /ui/` serves the shell (200, body contains a known marker); `GET /ui` 308-redirects to `/ui/`. Without `WithUI`, `GET /ui/` is 404 and the existing routes are unaffected (table-drive against the existing server test harness in `server_*_test.go`). |
| `cmd/orchestrator/main_test.go` | Add a case asserting `--enable-ui=false` (default) registers no `/ui/` route and `--enable-ui=true` does — reuse the existing flag-wiring test pattern. |

#### Key implementation details

- **Mirror the nil-safe gate exactly.** `WithUI` follows `WithChannels` / `WithLogBuffer`: absent option → the route is never registered → `/ui/` is a clean 404, the rest of the surface is untouched. No new "disabled" branch logic inside handlers.
- **`all:` embed pattern.** Vite emits a `.vite/manifest.json` and hashed asset names under dot-prefixed dirs in some configs; `//go:embed all:assets` ensures those are included so PR 3 needs no embed-directive change.
- **Hash-mode routing ([D1](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities))** means the static handler needs no `index.html` SPA-fallback shim — a plain `http.FileServer` is correct for every real-file request, and client routes live under `#`.
- **No middleware change.** `/ui/` registers on `s.mux`, which the existing `apiH` middleware stack already wraps; static serving inherits it. See [D3](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities) for the rate-limit caveat (deferred, not addressed here).

#### Tests

- Flag-on: `GET /ui/` → 200 with placeholder body; `GET /ui` → 308 → `/ui/`.
- Flag-off (default): `GET /ui/` → 404; `GET /healthz`, `GET /api/v1/agents` unaffected.
- `internal/ui` compiles with only the committed placeholder present (the Go-only-build guarantee).

#### PR checklist

- [ ] `go build ./...` green with **no** JS toolchain present (placeholder-only embed).
- [ ] `go test ./internal/server/... ./cmd/orchestrator/... -run UI -count=1` passes.
- [ ] `gofmt`/`go vet` clean; `golangci-lint` (or the repo's Go lint target) clean.
- [ ] `--enable-ui` defaults **off**; no default-runtime behaviour change.
- [ ] No `/api/v1/ui/*` handler in this PR (PR 2 owns them).

---

### PR 2: `feature/v036-rfc0048p1-config-context` — Feature-Toggle + Forward-Compat Endpoints

**Depends on**: PR 1 merged.
**Purpose**: Add the two read-only endpoints the SPA boots off — `/api/v1/ui/config` (which panels to render) and `/api/v1/ui/context` (who the principal is) — plus the `config/ui.yaml` toggle file, its JSON schema, and the `validate.py` map entry. This is the "make vertical slices real" mechanism: a panel ships dark in YAML and is flipped on per deployment.

#### Scope

| File | Change |
|------|--------|
| `internal/server/ui_handlers.go` (new) | `handleUIConfig`: returns `{panels: {<name>: {enabled, available}}, build: {version}}` per [RFC §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model). `enabled` comes from the loaded `config/ui.yaml` (or defaults); `available` is **runtime-derived** by the server from whether the backing subsystem is wired (e.g. `channel_timeline.available = (s.channelStore != nil)`, matching the existing 503 degradation) and is **never** read from YAML. `handleUIContext`: returns `{principal, tenant, authenticated}` — `{"principal":"local","tenant":"local","authenticated":false}` today ([RFC §F](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)). Registered in `registerRoutes` inside the same `s.uiFS != nil` block from PR 1. |
| [`internal/server/server.go`](../../internal/server/server.go) | Register `GET /api/v1/ui/config` and `GET /api/v1/ui/context` under the existing `if s.uiFS != nil {` block. Add a `uiConfig` field (parsed `config/ui.yaml`, or nil→defaults) populated via a new `WithUIConfig(...)` option **or** parsed from the config dir in `main.go` and passed through `WithUI` — see *Key details*. |
| [`cmd/orchestrator/main.go`](../../cmd/orchestrator/main.go) | When `--enable-ui`, load `<configDir>/ui.yaml` (absent → defaults; soft-degrade with an INFO log, mirroring [channels.go](../../cmd/orchestrator/channels.go)'s "not present → defaults" path) and pass the parsed toggles into the server. Resolve `build.version` from the existing version source used by `--version` / the version-bump tooling. |
| `internal/ui/config.go` (new, in `internal/ui` or `internal/server`) | `UIConfig` struct + loader: parse `panels.<name>.enabled`; absent file → the Slice-1 defaults (`chat`/`channel_timeline` on, `memory_strip`/`cost` off). `available` is **not** a field here — it is computed at request time. |
| `config/ui.yaml` (new) | Per [RFC §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model): `panels: {chat:{enabled:true}, channel_timeline:{enabled:true}, memory_strip:{enabled:false}, cost:{enabled:false}}` with the header comment that `available` is runtime-derived and must not appear. |
| `schemas/ui.schema.json` (new) | Draft-07 schema: `panels` object whose values are `{enabled: boolean}` with `additionalProperties:false` (so a stray `available` key fails validation, enforcing [D4](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities)/[RFC §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model)). Mirrors the structure of [channel.schema.json](../../schemas/channel.schema.json). |
| [`agents/validate.py`](../../agents/validate.py) | Add `"ui.yaml": "ui.schema.json"` to `_SCHEMA_MAP` (~line 26), so `make validate` checks the new file. Remove `ui.yaml` from the "TODO: add … when schemas exist" comment scope if listed. |
| `config/environments/*.yaml` | No new keys required for Slice 1; the override mechanism applies automatically if an operator adds a `ui:` block. (No edit unless a default differs per env — none does in Slice 1.) |
| `internal/server/ui_handlers_test.go` | Extend: `/api/v1/ui/config` shape; `available` reflects wired subsystems — channels wired → `channel_timeline.available=true`; channels absent → `false` (drive both with/without `WithChannels`). `/api/v1/ui/context` returns `principal=local`. Both 404 when `--enable-ui` is off (no `uiFS`). |

#### Key implementation details

- **`available` is server-derived, never authored.** The schema's `additionalProperties:false` on the per-panel object is the enforcement: a YAML author who writes `available:` fails `make validate`. This is the single source of the "ships dark → flip on when the subsystem is wired" contract.
- **Config pass-through shape.** Prefer threading the parsed `UIConfig` through a dedicated `WithUIConfig(cfg)` option rather than overloading `WithUI`, so the asset FS and the toggle config stay separable (a test can inject a config without an FS and vice versa). Either works; pick the one that keeps `server.go`'s option list uniform.
- **Unknown-panel forward-compat.** The handler returns whatever panels the config knows; the client ignores unknown panels ([RFC §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model)), so an older binary serving a newer bundle degrades gracefully. No server-side allow-list of panel names that would need editing per slice.
- **`/ui/context` is the single identity source.** Per [RFC §F rule 1](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility), the chat endpoint requires the client to *supply* `user_id`; the console must derive it from this endpoint's `principal`, never prompt for or hard-code it. The contract is set here so PR 4 consumes it.

#### Tests

- Config shape matches [RFC §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model) exactly (golden-ish assertion on the JSON keys).
- `available` true/false tracks `WithChannels` presence (the unit-level proof of the runtime-derivation contract).
- Absent `config/ui.yaml` → defaults (Slice-1 panels on, others off); malformed file → loud error at startup (matches channels.go reconcile-fail posture) **or** soft-degrade to defaults — pick the channels.go-consistent behaviour and assert it.
- `make validate` passes against `config/ui.yaml`; a `ui.yaml` carrying an `available:` key **fails** validation (negative test on the schema).

#### PR checklist

- [ ] `go test ./internal/server/... -run UI -count=1` passes (config shape + availability + context).
- [ ] `make validate` green; negative-test confirms `available:` in YAML is rejected.
- [ ] `agents/validate.py` `_SCHEMA_MAP` has the `ui.yaml` entry; `ruff`/`mypy` on `agents/validate.py` clean.
- [ ] Endpoints 404 when `--enable-ui` off.
- [ ] No write endpoints added; both new endpoints read-only and ride the existing middleware stack.

---

### PR 3: `feature/v036-rfc0048p1-spa-shell` — JS Toolchain + Svelte Shell + Build Step

**Depends on**: PR 2 merged.
**Purpose**: Introduce the repo's **first JS toolchain** and a Svelte SPA shell that boots off `/api/v1/ui/config` and `/api/v1/ui/context`, rendering empty slots for the `enabled && available` panels (the panels themselves land in PRs 4–5). Wire `make ui` and a CI build step that emit static assets into `internal/ui/assets/`, overwriting PR 1's placeholder.

#### Scope

| File | Change |
|------|--------|
| `web/` (new) | Svelte + Vite project: `package.json`, `vite.config.*` (set `base: "/ui/"` so asset URLs resolve same-origin under the subtree; output to `../internal/ui/assets`), `index.html`, `src/main.*`, `src/App.svelte`. Hash-mode router ([D1](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities)). Lockfile committed. |
| `web/src/lib/bootstrap.*` (new) | On load, `fetch("/api/v1/ui/config")` and `fetch("/api/v1/ui/context")`; store the enabled panels + principal in app state; render only `enabled && available` panels; ignore unknown panels. Derive `user_id` from `context.principal` (the [RFC §F rule 1](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility) single-source rule) and expose it to panels — no hard-coded user. |
| `web/src/panels/` (new) | Empty `Chat` and `ChannelTimeline` slot components (placeholder "coming in PR 4/5") so the shell renders the toggle wiring end-to-end before the panels exist. |
| [`Makefile`](../../Makefile) | New `ui` target: `cd web && npm ci && npm run build` (emits into `internal/ui/assets/`). Wire `ui` as a prerequisite of `build-orchestrator` (or a documented opt-in) so a release binary embeds the real bundle; keep a `build-orchestrator` path that works **without** `ui` for Go-only iteration (placeholder embed). Add `.PHONY: ui`. |
| CI workflow (`.github/workflows/*`) | Add a Node setup + `make ui` step before the Go build in the release/asset lane; the Go-only test lane keeps building against the placeholder. Cache `web/node_modules` keyed on the lockfile. |
| `.gitignore` (root) | Ignore `web/node_modules/`, `web/dist/` (if any intermediate), and confirm `internal/ui/assets/*` (except placeholder) stays ignored from PR 1. |
| `web/README.md` (new) | One-screen "how to build/run the console locally" for frontend contributors (the contributor-lane goal). |
| E2E smoke (preview/headless harness) | Shell loads at `/ui/`, fetches config + context, renders the two enabled panel slots, and shows no slot for `memory_strip`/`cost` (disabled). Asserts no hard-coded user — the principal comes from `/ui/context`. |

#### Key implementation details

- **`base: "/ui/"`** in Vite config is load-bearing: without it, the built `index.html` references `/assets/...` (root-absolute) and the same-origin `/ui/` subtree 404s every asset. Set it and verify against a flag-on binary.
- **Build writes the embedded dir.** `npm run build` outputs into `internal/ui/assets/`, replacing the placeholder; the `.gitignore` from PR 1 keeps the generated bundle untracked. A clean checkout has only the placeholder; a release build has the real bundle — they never coexist in git.
- **Go-only build stays green.** `make build-orchestrator` without `make ui` first compiles against the placeholder (PR 1's guarantee), so a Go contributor who never installs Node is unaffected. CI's release lane runs `make ui` first.
- **Minimal runtime, per OQ1.** Plain Svelte, no SvelteKit/SSR — the contract is "static files behind `WithUI`," so no Node runtime ships in the binary.
- **No new endpoints.** The shell consumes only PR 2's two endpoints; panels add their (existing) API calls in PRs 4–5.

#### Tests

- E2E: shell renders enabled slots only; disabled panels absent; principal read from `/ui/context`.
- Build determinism: `make ui` from a clean `web/` produces assets the flag-on binary serves (manual + CI gate).
- `make validate` / Go suites unaffected (no Go change beyond the build wiring).

#### PR checklist

- [ ] `make ui` builds clean from a fresh `web/` (lockfile committed).
- [ ] Flag-on binary built after `make ui` serves the real shell; built without it serves the placeholder.
- [ ] CI release lane runs Node setup + `make ui` before the Go build; Go-only lane unaffected.
- [ ] E2E smoke green: enabled-panel slots render, disabled panels absent, no hard-coded user.
- [ ] `web/node_modules` + generated assets gitignored; only the lockfile + source committed.

---

### PR 4: `feature/v036-rfc0048p1-chat-panel` — Chat Panel

**Depends on**: PR 3 merged. (Independent of PR 5.)
**Purpose**: Deliver the "feel the taste" moment — talk to a persona — over today's synchronous chat API. No backend change; pure SPA work against existing endpoints.

#### Scope

| File | Change |
|------|--------|
| `web/src/panels/Chat.svelte` (replace slot) | Persona picker from `GET /api/v1/agents` (+ `GET /api/v1/agents/{id}` for display name/role per [RFC §D.1](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero)). Send via `POST /api/v1/agents/{id}/chat` with `{message, user_id, session_id?, epoch_id?, participant_type:"user"}`; render `chatResponse.reply` (the `reply` field — confirmed in [internal/server/types.go](../../internal/server/types.go) `ChatResponse.Reply`). `user_id` derived from `/ui/context` principal (PR 2/3 contract), never prompted. Synchronous: show a "thinking…" state until the reply or a client timeout. |
| `web/src/panels/Chat.svelte` | Optional **session / epoch selectors** — the API already accepts `session_id` / `epoch_id` ([chat_handler.go](../../internal/server/chat_handler.go) RFC 0031 PR 4 / ISSUE-0085 PR 5 overrides), so the panel passes them through, quietly demonstrating the v0.3.5 isolation story ([RFC §D Session/epoch awareness](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero)). |
| `web/src/lib/api.*` | Thin typed client for the chat/agents calls; surfaces server error envelopes (`BAD_REQUEST` etc. from [chat_handler.go](../../internal/server/chat_handler.go)) as user-visible messages (e.g. message-too-long, bad `participant_type`). |
| E2E (preview/headless) | Pick a persona, send a message, see a reply; assert the request carries the `/ui/context`-derived `user_id` and `participant_type:"user"`; toggle a session selector and confirm it rides the request. |

#### Key implementation details

- **`reply` is the field** — `ChatResponse.Reply` serialises as `"reply"` ([types.go](../../internal/server/types.go)). Render that; surface `reply_status` only if non-nominal.
- **Client-side length guard** mirrors the server's `chatMaxMessageLength` rejection so the user gets immediate feedback before the round-trip (the server still enforces).
- **No streaming** (OQ5) — request/response with a "thinking…" affordance and a bounded client timeout that surfaces a retry, not a hang.
- **Identity rule** — `user_id` comes from `/ui/context`; the panel must not expose a free-text user field (would violate [RFC §F rule 1/2](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)).

#### Tests

- E2E happy path: persona picker populates, send → reply rendered.
- E2E error path: over-length message surfaces the server `BAD_REQUEST` without crashing the panel.
- E2E: session/epoch selector value appears in the outgoing request body.

#### PR checklist

- [ ] E2E: pick persona → send → see reply, green.
- [ ] `user_id` derived from `/ui/context`; no hard-coded or free-text user.
- [ ] `participant_type:"user"` sent; session/epoch overrides pass through when set.
- [ ] No Go/backend change in this PR.
- [ ] `make ui` clean; lint clean.

---

### PR 5: `feature/v036-rfc0048p1-channel-timeline` — Channel Timeline Panel

**Depends on**: PR 3 merged. (Independent of PR 4; may open before PR 4 merges.)
**Purpose**: Watch personas interact — render channel history newest-first and keep it live by polling (no channel-push API exists today, OQ4). Optional human publish so a tester can post into a group channel and watch agents respond.

#### Scope

| File | Change |
|------|--------|
| `web/src/panels/ChannelTimeline.svelte` (replace slot) | Channel list from `GET /api/v1/channels`; history from `GET /api/v1/channels/{id}/messages?limit=&before=` (params confirmed in [channel_query_params.go](../../internal/server/channel_query_params.go): `limit` positive int, `before` RFC-3339 cursor) rendered newest-first. |
| `web/src/panels/ChannelTimeline.svelte` | **Live-by-polling** per [RFC §D.2](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero): bounded interval (~2–3 s) appends new messages; **pause when the tab is backgrounded** (Page Visibility API) and **exponential backoff on errors** so an idle browser tab does not hammer the unauthenticated localhost surface. |
| `web/src/panels/ChannelTimeline.svelte` | **Optional human publish**: `POST /api/v1/channels/{id}/messages` to post into a group channel and watch the mention fan-out (RFC 0011) produce agent replies on the next poll. Gated so it is obviously a write action. |
| `web/src/lib/api.*` | Extend the typed client with channel list/history/publish; reuse the error-envelope surfacing from PR 4 (the `before`/`limit` parse errors are loud per [channel_query_params.go](../../internal/server/channel_query_params.go)). |
| E2E (preview/headless) | Open a channel, see history; post a message (or have a fixture agent post) and observe the polled timeline append it within one interval; assert polling pauses on a simulated backgrounded tab. |

#### Key implementation details

- **Cursor pagination** uses `before` (RFC-3339); the poll for *new* messages fetches the head (`limit` only) and de-dupes against the last-seen message id — do not re-render the whole history each tick.
- **Backoff + visibility-pause** are the load-protection contract called out in the RFC for the unauthenticated surface; both are acceptance-tested.
- **Publish is the one write** in Slice 1; it reuses the existing fan-out, adds no endpoint, and is the CSRF-relevant surface flagged in [RFC §Security](0048-operator-tester-web-console.md#security-considerations) — moot while unauthenticated, but the client is built to send whatever CSRF token/header the future auth layer chooses (documented in PR 6's guide, no code until auth exists).
- **`available:false` handling** — if `channel_timeline.available` is false (channels not wired), PR 3's shell already hides the slot; this panel assumes it only mounts when available.

#### Tests

- E2E: open channel → history renders newest-first; a new message appears within one poll interval.
- E2E: polling pauses on backgrounded tab; backs off after an injected error.
- E2E: optional publish posts and the agent reply appears on a later poll (against a fixture persona).

#### PR checklist

- [ ] E2E: open channel → see history → observe a polled update, green.
- [ ] Polling pauses when backgrounded and backs off on error (asserted).
- [ ] Head-poll de-dupes by message id; does not re-fetch full history per tick.
- [ ] No Go/backend change in this PR.
- [ ] `make ui` clean; lint clean.

---

### PR 6: `feature/v036-rfc0048p1-docs-closeout` — Docs + Status Closeout

**Depends on**: PR 4 and PR 5 merged.
**Purpose**: Ship the quick-start, the "try it in the browser" entry, the CHANGELOG/ROADMAP hygiene, the manual-test doc, and flip the RFC + this plan's status. Doc-only (no code).

#### Scope

| File | Change |
|------|--------|
| `docs/guides/web-console.md` (new) | Quick-start: `--enable-ui`, open `http://localhost:8080/ui`, pick a persona, chat, watch a channel. **Security note (load-bearing)**: the console makes the unauthenticated REST surface browser-discoverable; it binds `127.0.0.1` by default and exposing it beyond localhost **requires** fronting the orchestrator with an authenticating reverse proxy until RFC 0039 ships ([RFC §Security](0048-operator-tester-web-console.md#security-considerations)). Document the `config/ui.yaml` toggle model and the CSRF posture the future auth layer will impose. |
| [`README.md`](../../README.md) | "Try it in the browser" entry pointing at the guide. |
| [`ROADMAP.md`](../../ROADMAP.md) | **Add the missing RFC 0048 row** to the [RFC Master Index](../../ROADMAP.md#rfc-master-index) (0048 is absent at plan-authoring time — a hygiene gap) → status `⚠️ Partially Implemented`, target `v0.3.6 (Slice 1) + v0.4.0+ (Slices 2–5)`. Refresh `Last updated`. |
| [`CHANGELOG.md`](../../CHANGELOG.md) | New `[0.3.6]` (or `[Unreleased]`) section: "Embedded web console (RFC 0048 Slice 1) — chat + channel timeline behind `--enable-ui`, localhost-only." Upgrade note: console is off by default; do not expose beyond localhost without an auth proxy. |
| `docs/manual-tests/MT-CONSOLE-001.md` (new) | Fresh-stack script: build with `make ui`, run with `--enable-ui`, open the console, chat with a persona, reload, confirm the persona's recall is visible **in its chat replies** (the memory-strip recall check moves here once Slice 2 lands, per [RFC §Test Strategy](0048-operator-tester-web-console.md#test-strategy)); confirm session/epoch selectors scope correctly; confirm the channel timeline polls. Structural template: an existing `MT-*` doc. |
| [`docs/rfcs/0048-operator-tester-web-console.md`](0048-operator-tester-web-console.md) | Status `🚧 Implementing` → `⚠️ Partially Implemented (Phase 1 / Slice 1)`; append "Phase 1 / Slice 1 implemented in v0.3.6" to Decision/Next Steps. Update frontmatter `status:` so [INDEX.md](INDEX.md) regenerates via `make rfcs`. |
| [`docs/rfcs/INDEX.md`](INDEX.md) | Regenerated by `make rfcs` from the frontmatter change (do not hand-edit). |
| [`docs/rfcs/0048-phase1-pr-plan.md`](0048-phase1-pr-plan.md) | Fill the [Progress Overview](#progress-overview-phase-1) rows with merged-PR numbers + dates. |

#### Key implementation details

- **Status flips track the lifecycle, not this plan's authoring.** Per [RFC §Decision](0048-operator-tester-web-console.md#decision--next-steps), `Proposed → Accepted` required explicit review approval (given 2026-06-02; recorded in the plan-authoring PR, which set the RFC to **Accepted**). The RFC advances `Accepted → Implementing` when PR 1 opens. This PR (PR 6) lands the final `Implementing → Partially Implemented` flip, only after PRs 1–5 are merged and the slice is real.
- **`make rfcs-check`** must pass (frontmatter valid, INDEX not stale) — run `make rfcs` after editing the frontmatter, commit the regenerated INDEX.
- **ROADMAP row is genuinely new** — RFC 0048 was merged ([#493](https://github.com/mkhomutov/Persatrix/pull/493)) without a Master Index row; this PR adds it rather than editing an existing one.

#### PR checklist

- [ ] `docs/guides/web-console.md` published with the localhost-only / reverse-proxy security note.
- [ ] README "try it" entry links the guide.
- [ ] CHANGELOG `[0.3.6]` entry with the off-by-default upgrade note.
- [ ] ROADMAP RFC Master Index has a 0048 row (`⚠️ Partially Implemented`); `Last updated` refreshed.
- [ ] RFC 0048 frontmatter `status` flipped; `make rfcs-check` green; INDEX regenerated.
- [ ] `MT-CONSOLE-001` authored; execution deferred to v0.3.6 release-prep.
- [ ] This plan's Progress Overview rows filled.

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| `go:embed` on an empty `assets/` dir fails to compile, breaking `go build` for Go-only contributors and the Go CI lane before the JS build exists. | PR 1 commits a placeholder `index.html` ([D2](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities)); `internal/ui` always compiles. PR 1 checklist gates `go build ./...` with no JS toolchain. |
| The new JS toolchain (repo's 4th language) destabilises CI or blocks Go-only contributors. | The Go-only build/test lane builds against the placeholder; only the release/asset lane runs `make ui`. Node setup is cached on the lockfile. Frontend work is isolated to `web/`. |
| Vite `base` misconfig makes every asset 404 under the `/ui/` subtree. | PR 3 sets `base:"/ui/"` and the PR-3 checklist verifies a flag-on binary serves the real shell (not just `vite dev`). |
| Serving a browser console makes the **unauthenticated** REST surface more discoverable → accidental exposure. | `--enable-ui` defaults **off** (PR 1); orchestrator still binds `127.0.0.1`; PR 6's guide states plainly that beyond-localhost exposure requires an auth reverse proxy until RFC 0039. ([RFC §Security](0048-operator-tester-web-console.md#security-considerations)) |
| Polling hammers the localhost surface from idle/backgrounded tabs. | PR 5 implements visibility-pause + error backoff + head-poll de-dupe; the PR-5 checklist asserts all three. |
| Static `/ui/` asset fetches trip the shared rate limiter (many sub-resource requests per page load). | [D3](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities): not pre-optimised. If observed, exempt the `/ui/` prefix from the rate-limit middleware as a fast-follow — a localised change to the `apiH` wrap in [server.go](../../internal/server/server.go). |
| Browser-issued `POST`s (chat, channel publish) become CSRF targets the moment a cookie-auth proxy fronts the surface. | Moot while unauthenticated. [RFC §Security](0048-operator-tester-web-console.md#security-considerations) defers the CSRF mitigation to RFC 0039 / the fronting layer; PR 5's client is built to send whatever token/header that layer chooses, documented in PR 6's guide. |
| Memory strip scope-creeps back into Slice 1 (it depends on a new Go↔Python gRPC read method). | RFC decision (2026-06-02) defers it to Slice 2; PR 2 ships the `memory_strip` toggle **off** so it lands additively with no Slice-1 rework. ([RFC §G](0048-operator-tester-web-console.md#g-api-gaps--required-backend-work)) |
| `config/ui.yaml` silently unvalidated because `validate.py` skips unknown files. | PR 2 adds the `_SCHEMA_MAP` entry ([D4](#plan-authoring-decisions-resolve-rfc-implementation-ambiguities)); a negative test confirms an `available:` key in YAML fails `make validate`. |

---

## ROADMAP / Status Hygiene

Per [.github/copilot-instructions.md](../../.github/copilot-instructions.md) "Status Hygiene":

- **Plan-authoring PR** (review sign-off given 2026-06-02) → RFC 0048 `📋 Proposed → 👍 Accepted` (the only legal successor to Proposed; the plan is the "ready to implement" artifact, not yet implementation).
- **PR 1 opens** → RFC 0048 `👍 Accepted → 🚧 Implementing` (implementation is now actively in progress). It stays `Implementing` across PRs 1–5; the slice is not shippable until the panels merge. The v0.3.6 cycle, if a master plan is later authored, tracks per-PR progress; absent one, this plan's [Progress Overview](#progress-overview-phase-1) is the tracker.
- **PR 6 merges** → RFC 0048 `🚧 Implementing → ⚠️ Partially Implemented (Phase 1 / Slice 1)`; ROADMAP RFC Master Index row added; `Last updated` refresh; INDEX regenerated; CHANGELOG `[0.3.6]` entry.

---

## Future Slices (out of scope for Phase 1)

Tracking pointers only; each is its own design pass + PR plan ([RFC §E](0048-operator-tester-web-console.md#e-later-slices-roadmap)).

- **Slice 2 — Memory Inspector (v0.4.0+).** Read-only four-tier views with session/epoch scope filters. **Hard dependency**: a new read-only persona-memory endpoint = a Go handler **plus a new gRPC read method into the Python `agents/memory/` tiers** (a Go↔Python boundary crossing with its own scope-filter design — *not* a thin handler). The `memory_strip` toggle (shipped off in PR 2) flips on here with zero Slice-1 rework. ([RFC §G](0048-operator-tester-web-console.md#g-api-gaps--required-backend-work))
- **Slice 3 — Isolation Verifier (v0.4.0+).** Side-by-side session/epoch recall comparison + optional QA assert/diff harness. Depends on Slice 2.
- **Slice 4 — Cost & Observability (v0.4.0+).** `GET /api/v1/cost/summary` panel + live render of the existing SSE log stream (`/api/v1/executions/{id}/logs/stream`) + deep-links to Jaeger/Prometheus. No dependency beyond the Phase 1 scaffold; the `cost` toggle ships off in PR 2.
- **Slice 5 — Control Plane (post-RFC 0039).** Workflow runs, session/agent management, admin actions. **Hard gate**: RFC 0039 authentication — cannot ship enabled before auth exists. ([RFC §Security](0048-operator-tester-web-console.md#security-considerations))
- **Channel real-time (OQ4)** and **chat token streaming (OQ5)** — named later enhancements, revisited after Slice 1 lands.

---

## Progress Overview (Phase 1)

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 1 | `WithUI` ServerOption + embed package + static serving | `feature/v036-rfc0048p1-withui-scaffold` | ⬜ Not started | — | — |
| 2 | UI config/context endpoints + `config/ui.yaml` + schema | `feature/v036-rfc0048p1-config-context` | ⬜ Not started | — | — |
| 3 | JS toolchain + Svelte shell + build step | `feature/v036-rfc0048p1-spa-shell` | ⬜ Not started | — | — |
| 4 | Chat panel | `feature/v036-rfc0048p1-chat-panel` | ⬜ Not started | — | — |
| 5 | Channel timeline panel | `feature/v036-rfc0048p1-channel-timeline` | ⬜ Not started | — | — |
| 6 | Docs + status closeout | `feature/v036-rfc0048p1-docs-closeout` | ⬜ Not started | — | — |

---

## Related Documentation

- [RFC 0048 — Operator & Tester Web Console](0048-operator-tester-web-console.md) — canonical spec; this plan decomposes its Phase 1 / Slice 1.
- [RFC 0002 — REST API Server](0002-rest-api-server.md) — the chat/channels/agents/cost surface the console renders over.
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) — channel store + history endpoint + mention fan-out the timeline panel consumes.
- [RFC 0016 — Human Participant / Chat Interface](0016-human-participant-chat-interface.md) — the chat semantics Slice 1's chat panel drives.
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md) — the session/epoch axis the chat panel passes through.
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md) — hard gate for Slice 5; the auth boundary the same-origin embedding composes with.
- [RFC 0034 PR plan](0034-pr-plan.md) — structural template for this plan.
- [BRANCHING.md](../BRANCHING.md) — squash-merge, < 500-line-per-PR convention.
- `docs/guides/web-console.md` — quick-start (authored in PR 6).
- `docs/manual-tests/MT-CONSOLE-001.md` — fresh-stack console manual test (authored in PR 6).
