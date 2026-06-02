---
id: RFC-0048
title: "Operator & Tester Web Console (Vertical-Slice, Feature-Toggled UI)"
summary: "Embedded same-origin web console for operators and testers, shipped as feature-toggled vertical slices; first slice is live interactions (chat + channels) to grow the community."
type: feature
status: implementing
author: Maksim Khomutov
created: 2026-06-01
target: "v0.3.6 (Phase 1 / Slice 1) + v0.4.0+ (Phases 2–5)"
depends_on:
  - RFC-0002
  - RFC-0011
  - RFC-0016
---

# RFC 0048 — Operator & Tester Web Console (Vertical-Slice, Feature-Toggled UI)

**Type**: feature  
**Status**: 🚧 Implementing  
**Author**: Maksim Khomutov  
**Date**: 2026-06-01  
**Target**: v0.3.6 (Phase 1 / Slice 1) + v0.4.0+ (Phases 2–5)  
**Depends on**: RFC 0002 (REST API server), RFC 0011 (Channels & Bridges), RFC 0016 (Human Participant / Chat Interface)  
**Relates to**: RFC 0031 (Per-session namespacing), RFC 0033 (Model alias layer), RFC 0039 (User accounts & authentication)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Vocabulary](#a-vocabulary)
  - [B. Architecture & Delivery](#b-architecture--delivery)
  - [C. The Feature-Toggle Model](#c-the-feature-toggle-model)
  - [D. Slice 1 — Live Interactions (the hero)](#d-slice-1--live-interactions-the-hero)
  - [E. Later Slices (roadmap)](#e-later-slices-roadmap)
  - [F. Auth & Multi-Tenancy Forward-Compatibility](#f-auth--multi-tenancy-forward-compatibility)
  - [G. API Gaps & Required Backend Work](#g-api-gaps--required-backend-work)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

Persatrix today is operated entirely through the Rust CLI and external dashboards (Jaeger, Prometheus). This RFC proposes a **web console** for operators and testers, delivered as **independently shippable vertical slices** behind **per-panel feature toggles**. The console is a lightweight SPA **embedded into and served same-origin by the Go orchestrator** (`embed.FS`), so it inherits the orchestrator's security boundary and sidesteps CORS. The **first slice is live interactions** — chatting with a persona and watching a channel — because the fastest way to grow a community is to let people *feel the taste*: talk to a persona that remembers them, and watch personas interact. Later slices add memory inspection, session/epoch isolation verification, cost/observability, and control-plane actions.

## Motivation

The goal here is **community growth**, not internal tooling polish. People do not adopt an agent platform by reading a memory tier — they adopt it the moment they *interact* with a persistent persona and feel that it is not a stateless chatbot. The CLI is excellent for operators who already understand the system, but it is a poor first-contact surface: one command, one answer, no live view of a multi-agent channel, and no visceral demonstration of persistence and personality.

A web console lowers first-contact friction to "open a URL and talk." Critically, **most of the backend already exists**: RFC 0002 exposes a REST API (chat, channels, sessions, agents, cost) and an SSE log stream. A UI is therefore mostly a *render-over-existing-API* problem, not an architectural change — which is exactly why a thin first slice is achievable inside the v0.3.x line.

Doing nothing keeps the barrier to first contact at "clone the repo, build three toolchains, learn the CLI," which is fatal for community growth.

## Goals

1. Ship a **first vertical slice (Interactions)** in v0.3.x that lets a newcomer chat with a persona and watch a channel from the browser, with zero CLI knowledge.
2. Establish a **vertical-slice + feature-toggle architecture** so every future panel ships dark behind a flag and can be enabled per deployment.
3. Embed the UI **same-origin** in the orchestrator so it inherits the existing security boundary, needs no CORS, and composes cleanly with future auth (RFC 0039).
4. Make Persatrix's **differentiator visible** even in the first slice: persistence and personality, not "another chat box."
5. Keep the console **forward-compatible with authentication and multi-tenancy** from day one — no single-user / single-tenant assumptions baked into the client.
6. Preserve **single-binary distribution** ("download, run, open a URL") to maximize try-ability.

## Non-Goals

- **Not** a replacement for the CLI. The CLI remains the primary operator surface; the console is additive.
- **Not** a replacement for Jaeger/Prometheus/Grafana. Deep trace/metric analysis stays in those tools; the console links out to them.
- **Slice 1 is read-and-interact, not a full control plane.** Creating workflows, registering agents, and destructive admin actions are later slices, gated behind auth.
- **Not** shipping authentication or multi-tenancy itself — those land in RFC 0039 and its successors. This RFC only commits to *not blocking* them.
- **No token streaming or channel-push** in Slice 1 — chat is request/response and channel timelines poll, matching today's API. Streaming is a named later enhancement.

## Design / Implementation

### A. Vocabulary

| Term | Meaning |
|------|---------|
| **Console** | The web UI as a whole, served at `/ui` by the orchestrator. |
| **Panel** | A single self-contained UI feature area (e.g. Chat, Channel Timeline, Memory Inspector). |
| **Slice** | A vertical, independently shippable bundle of one or more panels plus any backend glue they need. Slice 1 = Interactions. |
| **Feature toggle** | A server-side flag that enables/disables a panel for a deployment; the SPA reads the set of enabled panels and renders only those. |
| **Console context** | Server-provided identity/tenant/capability info the SPA reads on load (degenerate `principal=local` today; real identity once RFC 0039 lands). |

### B. Architecture & Delivery

**Decision: a lightweight SPA, built to static assets, embedded into the Go orchestrator via `embed.FS`, served same-origin at `/ui`, gated by a `--enable-ui` flag and a `WithUI` `ServerOption`.**

This follows the orchestrator's established optional-subsystem pattern (channels, cost, and the log buffer are each wired via a functional `ServerOption` and degrade to a nil-safe `503` when absent — see `cmd/orchestrator/channels.go` and `internal/server/server.go`).

Why same-origin embedding wins, grounded in the current code:

- The REST surface has **no CORS middleware** today. A separate-origin SPA would be blocked by the browser unless we add CORS or front everything with a proxy. Same-origin needs neither.
- The REST surface has **no authentication** yet. The in-code posture (`agent_handlers.go`) documents the surface as unauthenticated *"until token validation lands in RFC 0009 Phase 4"*; the *human* identity axis the console actually needs is carried by RFC 0039 (User Accounts & Authentication), which explicitly reframes RFC 0009 as the agent-identity counterpart and takes human auth as its own scope. Either way, serving the UI from the same origin means there is exactly **one** boundary to secure later, and the future auth layer (or a fronting proxy) covers `/ui` and `/api` together.
- **Single-binary distribution** is preserved — the static assets live inside the orchestrator binary, so "download, run, open `http://localhost:8080/ui`" works with no separate frontend deploy.

**Alternatives considered:**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Embedded SPA via `embed.FS` (chosen)** | Same-origin (no CORS), one security boundary, single binary, rich interactivity, opens a frontend-contributor lane | Adds a JS build step + a 4th language to the repo | **Chosen** |
| Server-rendered (templ + htmx) in Go | No JS toolchain, stays in Go | Weaker fit for live chat/channel interactivity; reinvents component ergonomics; still a new dependency | Rejected for the hero slice; reconsider for static panels |
| Separate-origin SPA (own deploy) | Decoupled release cadence | Forces CORS + credentialed cross-origin auth now; breaks single-binary try-ability | Rejected |
| Reuse Grafana/Jaeger only | Cheapest | Cannot show memory-tier, isolation, or interactive chat — none are metrics | Rejected (insufficient) |

**Framework**: **Svelte** (plain Svelte + Vite, emitting plain static files) — chosen for the smallest mental model and minimal runtime, best suited to the community-growth / frontend-contributor-lane goal (see [Open Questions](#open-questions) for the resolved trade-off vs Preact/SolidJS). The architecture does not depend on this choice, since the contract is "static assets embedded behind `WithUI`."

**Serving sketch** (mirrors existing nil-safe handler gating):

```go
// internal/server/server.go
func WithUI(uiFS fs.FS) ServerOption { return func(s *Server) { s.uiFS = uiFS } }

// in registerRoutes, only when wired:
if s.uiFS != nil {
    s.mux.Handle("GET /ui/", http.StripPrefix("/ui/", http.FileServer(http.FS(s.uiFS))))
    s.mux.HandleFunc("GET /api/v1/ui/config", s.handleUIConfig)
    s.mux.HandleFunc("GET /api/v1/ui/context", s.handleUIContext)
}
```

```go
// cmd/orchestrator/main.go
if *enableUI {
    srvOpts = append(srvOpts, server.WithUI(ui.Assets())) // ui.Assets() returns an embed.FS sub-tree
}
```

A bare `http.FileServer` over the `embed.FS` returns `404` for client-side routes that are not real files (e.g. reloading `/ui/chat`). If the SPA uses history-mode routing, the `/ui/` handler must fall back to serving `index.html` for unmatched paths; hash-mode routing sidesteps this. (`GET /ui` without the trailing slash is fine — `net/http` redirects it to `/ui/` for the subtree pattern.)

### C. The Feature-Toggle Model

Each panel is gated independently so slices ship dark and operators opt in per deployment.

- **Server-side source of truth.** `GET /api/v1/ui/config` returns the set of enabled panels plus per-panel availability derived from whether the backing subsystem is wired (e.g. the Channel Timeline panel reports `available:false` if `channels.yaml` is absent, matching the existing `503` degradation). Shape:

  ```json
  {
    "panels": {
      "chat":            { "enabled": true,  "available": true },
      "channel_timeline":{ "enabled": true,  "available": true },
      "memory_strip":    { "enabled": false, "available": false },
      "cost":            { "enabled": false, "available": true }
    },
    "build": { "version": "0.3.6" }
  }
  ```

- **Client behavior.** On load the SPA fetches `/api/v1/ui/config` and renders only `enabled && available` panels. Unknown panels are ignored, so an older binary serving a newer asset bundle (or vice versa) degrades gracefully.
- **Toggle source.** Panel enablement is set via **`config/ui.yaml`** alongside `--enable-ui`, consistent with the existing `config/*.yaml` convention (`channels.yaml`, `bridges.yaml`, `agents.yaml`) and its absent-file→`503` degradation, with `config/environments/*` overrides applying as they do for other config. Defaults: Slice 1 panels on, everything else off.

  The file carries only the operator-controlled `enabled` flag per panel; `available` is **runtime-derived** by the server (from whether the backing subsystem is wired) and is never authored in YAML. Absent file → defaults below. Validated against `schemas/ui.schema.json` via `make validate`, like the other config files.

  ```yaml
  # config/ui.yaml — Web Console panel toggles (RFC 0048, v0.3.6+).
  # `available` is derived at runtime and must NOT appear here.
  panels:
    chat:             { enabled: true }   # Slice 1
    channel_timeline: { enabled: true }   # Slice 1
    memory_strip:     { enabled: false }  # Slice 2 (ships dark)
    cost:             { enabled: false }  # Slice 4 (ships dark)
  ```

This is the mechanism that makes "vertical slices" real: a half-built Memory Inspector can merge to `main` shipped-dark, exercised in tests, and flipped on only when ready.

### D. Slice 1 — Live Interactions (the hero)

Slice 1 delivers the "feel the taste" moment with two shipping panels (a third, the memory strip, is deferred to Slice 2 — see below), all over today's API.

**1. Chat panel** — talk to a persona.
- `GET /api/v1/agents` to populate a persona picker; `GET /api/v1/agents/{id}` for display name/role.
- `POST /api/v1/agents/{id}/chat` with `{ message, user_id, session_id?, epoch_id?, participant_type:"user" }`; render `chatResponse.reply`.
- Chat is **synchronous request/response** (no token streaming yet) — the panel shows a "thinking…" state until the reply or timeout returns. Token streaming is deferred (see Open Questions).

**2. Channel timeline panel** — watch personas interact.
- `GET /api/v1/channels` to list channels; `GET /api/v1/channels/{id}/messages?limit=&before=` to render history newest-first.
- **Live view by polling** (no channel-push API exists today): the panel polls the messages endpoint on a bounded interval (default ~2–3 s) and appends new messages, pausing polling when the tab is backgrounded and backing off on errors so idle browser tabs do not hammer the unauthenticated localhost surface. A real-time channel SSE/WebSocket is a named later enhancement, not a Slice-1 dependency.
- Optional: `POST /api/v1/channels/{id}/messages` to let a human post into a group channel and watch agents respond (reuses the mention fan-out already in RFC 0011).

**3. Memory strip — deferred to Slice 2 (decided).**
- The intended panel: a compact "what I remember about you" strip beside the chat — the persona's relationship/trust standing and a few recalled facts/notes about the current `user_id` — the cheapest way to prove "not a chatbot."
- **Decision (2026-06-02): deferred to Slice 2.** It depends on a read-only memory endpoint that does not exist yet, and that endpoint is a new gRPC read method into the Python `agents/memory/` tiers (a Go↔Python boundary crossing with its own scope-filter design), not a thin handler — see [API Gaps](#g-api-gaps--required-backend-work). Deferring keeps Slice 1 (chat + channel timeline) small and shippable in v0.3.6.
- The strip remains behind its own `memory_strip` toggle, so it lands in Slice 2 with **zero rework** to Slice 1. In the meantime, the persona's recall is already visible *in its chat replies*.

**Session/epoch awareness**: the chat panel exposes optional session and epoch selectors (the API already accepts `session_id`/`epoch_id`), so even Slice 1 quietly demonstrates the v0.3.5 isolation story for curious testers.

### E. Later Slices (roadmap)

Sketched here to show the architecture scales; each is its own design pass.

- **Slice 2 — Memory Inspector**: read-only views of the four tiers (episodic / relationships / facts / notes) per persona, with session/epoch scope filters. The tester's highest-value view.
- **Slice 3 — Isolation Verifier**: side-by-side "what is recallable in session A vs B / epoch N vs N+1," turning manual MT isolation reports into observation. Optional assert/diff harness for QA.
- **Slice 4 — Cost & Observability**: `GET /api/v1/cost/summary` panel + the existing SSE log stream (`/api/v1/executions/{id}/logs/stream`) rendered live, with deep-links out to Jaeger/Prometheus.
- **Slice 5 — Control Plane**: run workflows, manage sessions, register/reload agents. **Write-heavy → gated behind RFC 0039 auth.**

### F. Auth & Multi-Tenancy Forward-Compatibility

The system will gain authentication and multi-tenancy (RFC 0039 and successors). The console must not paint that into a corner. The schema already carries the seed: ISSUE-0081 (PR 3) added `principal_id TEXT NOT NULL DEFAULT 'local'` across the **persona-memory tiers** (the Python memory layer — see `agents/principal_id.py`). Note this axis lives at the memory tier and is **not yet surfaced on the REST/chat API the console consumes** — today that surface carries only `user_id`, no explicit principal/tenant field. We treat **today's no-auth localhost mode as the degenerate single-tenant case (`principal=local`)**, not as the permanent shape.

Concrete forward-compat rules for Slice 1:

1. **Identity is server-provided, never assumed.** The SPA reads `GET /api/v1/ui/context` for the current principal/tenant and capability hints. Today it returns `{ "principal": "local", "tenant": "local", "authenticated": false }`. When RFC 0039 lands, the same endpoint returns the real identity and the client needs no structural change. Note the chat endpoint requires the client to *supply* `user_id` as a request field; in local mode the console must derive that value from the `/ui/context` principal rather than prompting for or hard-coding it, so there is a single server-provided identity source even before auth exists.
2. **No client-side single-user assumptions.** Persona lists, channels, sessions, and memory views are always rendered as "what *this* principal/tenant can see," even when that is everything (local mode). No global caches keyed without the tenant axis.
3. **Ride the tenant axis the backend already namespaces by.** Session/epoch scoping (RFC 0031) *is* exposed on the REST surface today, so the client passes it through rather than flattening it. The `principal_id` axis exists at the memory tier but is **not yet plumbed through the REST/chat API** — until it is, the client reads principal/tenant from `/api/v1/ui/context` and supplies `user_id` on chat, and must avoid any single-principal assumption so that wiring the principal axis through later is purely additive.
4. **Same-origin = one auth boundary.** Because `/ui` and `/api` share an origin, the future auth layer (cookie/session or a fronting authenticating proxy) covers both at once with no CORS-with-credentials complexity.
5. **Write slices are auth-gated by construction.** Slice 5 (control plane) declares a hard dependency on RFC 0039; it cannot ship enabled before auth exists.

### G. API Gaps & Required Backend Work

What Slice 1 needs that the backend does **not** yet provide:

| Gap | Needed for | Proposed resolution |
|-----|-----------|---------------------|
| Static asset serving / `embed.FS` | Serving the SPA at all | New `WithUI` `ServerOption` + `--enable-ui` flag (Phase 1) |
| `GET /api/v1/ui/config` | Feature toggles | New lightweight handler returning enabled/available panels (Phase 1) |
| `GET /api/v1/ui/context` | Auth/tenant forward-compat | New handler; returns `principal=local` today (Phase 1) |
| Read-only persona-memory endpoint (e.g. `GET /api/v1/agents/{id}/memory?user_id=`) | Memory strip (Slice 2) | New Go handler **plus a new gRPC read method into the Python persona runtime** — the recall/relationship tiers live in `agents/memory/`, not the Go server, so this crosses the Go↔Python boundary and is more than a thin handler; **scope to Slice 2 if it pressures Phase 1** |
| Channel live-push (SSE/WebSocket) | Real-time channel timeline | **Deferred** — poll the existing messages endpoint in Slice 1; design a channel SSE later, reusing the log-stream SSE pattern |
| CORS middleware | Only if a separate-origin deploy is ever wanted | **Not needed** for embedded same-origin; revisit only if a decoupled deploy is requested |

Everything else Slice 1 uses (chat, channel list/history/publish, agent list/info, sessions) already exists per RFC 0002 / RFC 0011 / RFC 0016.

## Security Considerations

- **The entire REST surface is unauthenticated until RFC 0039.** Serving a browser console makes that surface more *discoverable and usable*, which raises the stakes of accidental exposure. Mitigations: `--enable-ui` defaults **off**; when on, the orchestrator continues to bind `127.0.0.1` by default; documentation must state plainly that exposing the console beyond localhost requires fronting the orchestrator with an authenticating reverse proxy until RFC 0039 ships.
- **Write-capable panels are gated.** Slice 1 is interact-only against existing endpoints; Slice 5 (control plane, destructive/admin actions) declares a hard dependency on RFC 0039 and cannot be enabled before auth exists.
- **No new privileged endpoints.** The only new endpoints (`/api/v1/ui/config`, `/api/v1/ui/context`, and the optional read-only memory endpoint) are read-only and must respect the same rate-limiting/audit middleware already applied to the REST surface.
- **Multi-tenancy isolation is a backend invariant.** The console must never become a side channel that bypasses tenant scoping; all data it shows flows through the same scoped APIs, so isolation is enforced server-side, not in the client.
- **CSRF on browser-driven writes.** Slice 1 introduces browser-issued `POST`s (chat, optional human channel-publish) to a surface that today has no auth and no CSRF defense. While the surface is unauthenticated this is moot, but the moment the recommended exposure path lands — a fronting proxy adding *cookie/session* auth — those same-origin `POST`s become CSRF targets. The auth design (RFC 0039 / the fronting layer) must pair cookie auth with a CSRF mitigation (SameSite cookies, a CSRF token, or a custom-header/Origin check); the console should be built to send whichever the auth layer chooses. Flagged here because the console is what first makes these endpoints reachable from a browser.
- **Static assets** are embedded (not user-uploaded) and served read-only; no path traversal risk beyond what `http.FileServer` over an `embed.FS` already prevents.

## Phased Implementation Plan

### Phase 1: Slice 1 — Interactions (v0.3.6)

**Summary**: Stand up the embedded-UI scaffold and ship the Interactions slice (chat + channel timeline; the memory strip is deferred to Slice 2, its toggle shipping off).

**Deliverables**:
1. `WithUI` `ServerOption` + `--enable-ui` flag + `embed.FS` asset wiring (`internal/server/`, `cmd/orchestrator/`, new `internal/ui/` or `web/`).
2. `GET /api/v1/ui/config` (feature toggles) and `GET /api/v1/ui/context` (auth/tenant forward-compat, `principal=local`).
3. SPA scaffold (chosen framework) with the toggle/context bootstrap.
4. **Chat panel** over `POST /api/v1/agents/{id}/chat` with persona picker and optional session/epoch selectors.
5. **Channel timeline panel** over `GET /api/v1/channels/{id}/messages` with interval polling + optional human publish.
6. ~~Memory strip~~ — **deferred to Slice 2 (decided 2026-06-02)**; the `memory_strip` toggle ships defaulting off, so the panel lands in Slice 2 with no rework to 1–5.
7. Docs: a `docs/guides/web-console.md` quick-start; README "try it in the browser" entry.

**Dependencies**: RFC 0002, RFC 0011, RFC 0016 (all implemented). No dependency on RFC 0039.

### Phase 2: Slice 2 — Memory Inspector (v0.4.0+)
**Summary**: Read-only four-tier memory views with session/epoch scope filters. **Dependencies**: read-only memory endpoint from Phase 1's gap list.

### Phase 3: Slice 3 — Isolation Verifier (v0.4.0+)
**Summary**: Side-by-side session/epoch recall comparison + optional QA assert/diff harness. **Dependencies**: Phase 2.

### Phase 4: Slice 4 — Cost & Observability (v0.4.0+)
**Summary**: Cost summary panel + live log SSE rendering + deep-links to Jaeger/Prometheus. **Dependencies**: none beyond Phase 1 scaffold.

### Phase 5: Slice 5 — Control Plane (post-RFC 0039)
**Summary**: Workflow runs, session/agent management, admin actions. **Dependencies**: **Hard gate** — RFC 0039 authentication.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Go orchestrator | `internal/server/server.go`, `internal/server/ui_handlers.go` (new) | `WithUI` option, `/ui` static serving, `/api/v1/ui/config` + `/api/v1/ui/context` handlers |
| Go orchestrator | `cmd/orchestrator/main.go` | `--enable-ui` flag + conditional `WithUI` wiring |
| Config | `config/ui.yaml` (new), `schemas/ui.schema.json` (new), `config/environments/*` | Panel-toggle file + its JSON schema (wired into `make validate`); env overrides follow the existing pattern |
| Web console | `web/` or `internal/ui/` (new) | SPA source + build config + `embed.FS` package exposing `Assets()` |
| Go orchestrator (optional) | `internal/server/memory_handlers.go` (new) + persona-runtime gRPC service (`agents/`) | Read-only persona-memory endpoint for the memory strip / Slice 2 — Go handler **and** a new gRPC read method into the Python memory tiers (`agents/memory/`) |
| Docs | `docs/guides/web-console.md` (new), `README.md`, `ROADMAP.md` | Quick-start, try-it entry, roadmap slot |
| Build | `Makefile`, CI | UI build step producing embedded static assets |

## Test Strategy

- **Unit tests**: `ui_handlers` config/context shape; panel availability reflects wired subsystems (channels present/absent → `available` true/false).
- **Integration tests**: with `--enable-ui`, `GET /ui/` serves the SPA shell; `/api/v1/ui/config` and `/api/v1/ui/context` return expected JSON; without the flag, `/ui` and the ui-endpoints are absent (404), other routes unaffected.
- **E2E / smoke**: headless-browser flow — pick a persona, send a chat, see a reply; open a channel, see history, observe a polled update. (Preview/headless harness.)
- **Manual tests**: a `docs/manual-tests` script — fresh stack, open the console, chat with a persona, reload, confirm the persona's recall is visible in its chat replies; confirm session/epoch selectors scope correctly. (The memory-strip recall check moves here once Slice 2 lands.)
- **Forward-compat check**: assert the SPA reads identity from `/api/v1/ui/context` and renders correctly for `principal=local`, with no hard-coded single-user assumptions.

## Open Questions

1. **SPA framework** — Svelte vs SolidJS vs Preact (minimal-runtime, static build). Does not affect the embedding contract.
   **Resolution**: **Svelte (decided 2026-06-02)** — smallest mental model + minimal runtime, best fit for the community-growth/contributor-lane goal. Preact (React-compatible, largest contributor pool) was the runner-up and stays the fallback if React familiarity later outweighs simplicity; the framework-agnostic `WithUI` contract keeps the switch cheap.
2. **Toggle surface** — `ui.yaml` config file vs repeated `--ui-panel=` flags vs env vars for per-panel enablement.
   **Resolution**: **`config/ui.yaml` (decided 2026-06-02)** — consistent with the existing `config/*.yaml` convention (`channels.yaml`, `bridges.yaml`, `agents.yaml`) and the `config/environments/*` override mechanism.
3. **Read-only memory endpoint** — land in Phase 1 (enables the memory strip) or defer to Slice 2? Shape and scope filters (`user_id`, `session_id`, `epoch_id`).
   **Resolution**: **Deferred to Slice 2 (decided 2026-06-02)** — it is a new gRPC read method into the Python `agents/memory/` tiers, not a thin handler, and deserves its own design pass; deferring keeps Slice 1 shippable in v0.3.6. The `memory_strip` toggle ships off so the panel lands additively.
4. **Channel real-time** — design a channel SSE/WebSocket (reusing the log-stream SSE pattern) as a fast-follow, or keep polling indefinitely for Slice 1?
   **Resolution**: deferred — Slice 1 polls; revisit after Slice 1 lands.
5. **Chat token streaming** — the chat endpoint is synchronous today; is a streaming variant worth adding for UI feel, and when?
   **Resolution**: deferred — Slice 1 uses request/response.

## Decision / Next Steps

Status is **Implementing** (review sign-off given 2026-06-02; Phase 1 PR plan authored). The decisions requested before Phase 1 are now resolved (2026-06-02):

- **Embedded same-origin SPA (Section B): confirmed.**
- **OQ1 — framework: Svelte.**
- **OQ2 — toggle surface: `config/ui.yaml`.**
- **OQ3 — read-only memory endpoint / memory strip: deferred to Slice 2.** Slice 1 ships chat + channel timeline.
- **Slice-1 target: v0.3.6, confirmed.**
- **OQ4 (channel real-time) and OQ5 (chat token streaming): remain deferred** — Slice 1 polls and uses request/response chat.

With these settled and review sign-off given (2026-06-02), the RFC advances to **Implementing**. The Phase 1 PR plan — [`0048-phase1-pr-plan.md`](0048-phase1-pr-plan.md) — decomposes the scaffold + Interactions slice (chat + channel timeline) into six reviewable PRs and is the active tracker for the build. Status advances to **Partially Implemented** once Slice 1 lands (its closeout PR per the plan).

## Related Documentation

- [RFC 0002 — REST API Server](0002-rest-api-server.md)
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md)
- [RFC 0016 — Human Participant / Chat Interface](0016-human-participant-chat-interface.md)
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md)
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md)
- [Sessions guide](../guides/sessions.md)
- [Epochs guide](../guides/epochs.md)
