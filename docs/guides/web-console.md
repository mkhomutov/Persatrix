# Web Console — Operator & Tester Guide

The **web console** is an embedded, same-origin browser UI for the orchestrator:
open a URL, pick a persona, chat with it, and watch a channel — with zero CLI
knowledge. It is the first vertical slice (Interactions) of
[RFC 0048](../rfcs/0048-operator-tester-web-console.md), shipped in **v0.3.6**
behind the `--enable-ui` flag (**default off**).

> **Read this first — the security note below is load-bearing.** Enabling the
> console makes the orchestrator's **unauthenticated** REST surface
> browser-discoverable. The orchestrator binds `127.0.0.1` by default and the
> console is off by default; **do not expose it beyond localhost** until the
> REST surface is authenticated. See [§ Security](#security--do-not-expose-beyond-localhost).

> **Spec-level detail** lives in [RFC 0048](../rfcs/0048-operator-tester-web-console.md)
> (§B same-origin embedding, §C the feature-toggle model, §D Slice 1, §Security).
> The implementation workstream is the [Phase 1 PR plan](../rfcs/0048-phase1-pr-plan.md).
> This guide is deliberately non-exhaustive and points into both for rationale.

---

## Table of Contents

- [What it is](#what-it-is)
- [Quick start (local binary)](#quick-start-local-binary)
- [Quick start (Docker demo)](#quick-start-docker-demo)
- [The Chat panel](#the-chat-panel)
- [The Channel-timeline panel](#the-channel-timeline-panel)
- [Creating a channel (opt-in)](#creating-a-channel-opt-in)
- [The feature-toggle model (`config/ui.yaml`)](#the-feature-toggle-model-configuiyaml)
- [Security — do not expose beyond localhost](#security--do-not-expose-beyond-localhost)
- [What is not in Slice 1](#what-is-not-in-slice-1)
- [Troubleshooting](#troubleshooting)

---

## What it is

The console is a small Svelte single-page app served as static assets embedded
into the Go orchestrator binary (`embed.FS`) — there is **no separate web
server and no Node runtime in the deployed binary**. It renders over the REST
API that already exists (chat per [RFC 0016](../rfcs/0016-human-participant-chat-interface.md),
channels per [RFC 0011](../rfcs/0011-channels-bridges.md), agents per
[RFC 0002](../rfcs/0002-rest-api-server.md)), so Slice 1 is mostly a
*render-over-existing-API* surface, not new backend behaviour.

On load the app boots off two read-only endpoints:

- `GET /api/v1/ui/config` — which panels to render (`enabled` from
  `config/ui.yaml`, `available` derived at runtime from whether the backing
  subsystem is wired) plus the build version.
- `GET /api/v1/ui/context` — who the principal is. Today this is the degenerate
  single-tenant case `{"principal":"local","tenant":"local","authenticated":false}`;
  the Chat panel derives its `user_id` from this `principal` — it is **never**
  hard-coded or free-text typed (the [RFC §F](../rfcs/0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)
  single-identity-source rule, so the console composes with RFC 0039 auth later).

A panel renders only when it is **both** `enabled` (operator toggle) **and**
`available` (subsystem wired). Unknown panels are ignored, so an older binary
serving a newer bundle degrades gracefully.

---

## Quick start (local binary)

```bash
# 1. Build the orchestrator with the real Svelte bundle embedded, then run it
#    with the console enabled (binds 127.0.0.1 by default).
make run-ui

# 2. Open the console in a browser:
#    http://localhost:8080/ui
```

`make run-ui` builds the UI bundle (`make ui`) and the orchestrator, then runs
it with `--enable-ui`. To wire the flag onto your own invocation instead:

```bash
make ui                                   # build web/ → internal/ui/assets/
make build-orchestrator                   # embed the bundle
./bin/orchestrator --enable-ui            # serve /ui (still localhost-only)
```

> **Go-only contributors need no JS toolchain.** `go build ./...` and
> `make build-orchestrator` compile against a committed placeholder asset, so a
> contributor who never installs Node is never blocked. A flag-on binary built
> *without* `make ui` serves a clear "run `make ui`" placeholder instead of the
> real console. Only `make ui` / the Docker image build / the CI release lane
> produce the real bundle.

With `--enable-ui` **off** (the default), `/ui/` is a clean `404` and no
default runtime behaviour changes.

---

## Quick start (Docker demo)

The demo compose stack enables the console out of the box and bakes the real
Svelte bundle into the image (a Node build stage inside
`Dockerfile.orchestrator`), so it works from a clean clone with **no host JS
toolchain**:

```bash
make demo-offline          # or demo-ollama / docker compose up --build
# open http://localhost:8080/ui
```

The compose stack publishes `:8080` for local use only. See
[§ Security](#security--do-not-expose-beyond-localhost) before changing that
publish or the bind address.

---

## The Chat panel

The hero moment — talk to a persona over the synchronous chat API:

1. Pick a persona from the list (`GET /api/v1/agents`).
2. Type a message and send it (`POST /api/v1/agents/{id}/chat` with
   `participant_type:"user"` and the `user_id` derived from `/ui/context`).
3. Read the reply (the `reply` field of the response). A "thinking…"
   affordance shows until the reply lands or a client timeout surfaces a retry.

**Optional session / epoch selectors** pass `session_id` / `epoch_id` through
to the request, so you can demonstrate the v0.3.5 isolation story from the
browser: switch the [epoch](epochs.md) and the same persona answers from a
clean slate; switch the [session](sessions.md) and it answers from a different
room's memory. Leave them unset for the default room.

Over-length messages are caught client-side (mirroring the server's
4 000-character limit) and the server's error envelope is surfaced as a
user-visible message rather than crashing the panel.

---

## The Channel-timeline panel

Watch personas interact:

1. Pick a channel (`GET /api/v1/channels`).
2. History renders newest-first (`GET /api/v1/channels/{id}/messages`).
3. The timeline stays **live by polling** (no channel push API exists yet —
   [OQ4](../rfcs/0048-operator-tester-web-console.md#open-questions) is deferred):
   a bounded interval appends new messages, **pauses when the tab is
   backgrounded** (Page Visibility API), **backs off on errors**, and
   **de-dupes** by polling the head against the last-seen message id rather than
   re-rendering the whole history each tick — so an idle tab does not hammer the
   unauthenticated localhost surface.
4. **Optional human publish** (`POST /api/v1/channels/{id}/messages`) posts into
   a group channel; the [RFC 0011](../rfcs/0011-channels-bridges.md) mention
   fan-out surfaces the agent replies on the next poll.

---

## Creating a channel (opt-in)

The Channels panel can also **create** a group channel from the browser — so you
can spin one up, drop two personas in it, and watch them interact without leaving
the console for the CLI or hand-editing
[`config/channels.yaml`](../../config/channels.yaml). It surfaces the existing
`POST /api/v1/channels` endpoint; **no new backend surface is added**
([RFC 0048 channel-creation amendment](../rfcs/0048-amendment-channel-creation.md)).

It ships **dark** and is a **structural write before auth**, so it is gated on
two conditions — read the [Security](#security--do-not-expose-beyond-localhost)
note before enabling it.

**To enable it:**

1. **Opt in** in [`config/ui.yaml`](../../config/ui.yaml) — add `create_enabled`
   under the `channel_timeline` panel:

   ```yaml
   panels:
     channel_timeline:
       enabled: true
       create_enabled: true   # NEW — ships false; this opts into channel creation
   ```

   `create_enabled` is the only new authored knob, and it defaults to `false`.

2. **Run with channels wired.** Just like the panel's own `available` flag, the
   create affordance's `create.available` is **runtime-derived** — true only when
   the channel store is wired. With channels unconfigured the button stays hidden
   even with the toggle on. (`create.available` is never authored; an
   `available:` key in the YAML is a `make validate` error.)

3. **Use it.** In the **Channels** tab, click **New channel** (beside Refresh),
   enter a name (the server derives the canonical `group:<name>` id, shown
   read-only — do not type the `group:` prefix yourself), an optional
   description, and pick members from the registered-agent list with a per-member
   respond policy (`when_mentioned` (default) / `always` / `never`). On success
   the picker reloads and selects the channel you just made.

**Verify the toggle is live:**

```bash
curl -s http://localhost:8080/api/v1/ui/config | jq '.panels.channel_timeline'
# want: { "enabled": true, "available": true,
#         "create": { "enabled": true, "available": true } }
```

Both `create.enabled` **and** `create.available` must be `true` for the **New
channel** affordance to render — the same `enabled && available` rule every panel
follows.

> **Scope.** Only `group:` channels are creatable. DMs and threads are created
> implicitly on first message ([RFC 0011](../rfcs/0011-channels-bridges.md)), so
> there is nothing to "create" for those. Channel **deletion** and post-create
> membership editing are not in Slice 1.

---

## The feature-toggle model (`config/ui.yaml`)

Panels ship "dark" in [`config/ui.yaml`](../../config/ui.yaml) and are flipped
on per deployment. Slice 1 ships the two hero panels on; later slices ship off
so they land additively:

```yaml
panels:
  chat:
    enabled: true
  channel_timeline:
    enabled: true
    create_enabled: false   # opt-in to channel creation — see "Creating a channel"
  memory_strip:        # Slice 2 (v0.4.0+) — ships off
    enabled: false
  cost:                # Slice 4 (v0.4.0+) — ships off
    enabled: false
```

Two rules make this real:

- **`enabled` is the operator knob** (and `create_enabled` is a per-panel
  capability knob alongside it). They decide whether the console *offers* a panel
  or affordance.
- **`available` is runtime-derived and never authored.** The server computes,
  per request, whether each panel's backing subsystem is wired (e.g.
  `channel_timeline.available` is true exactly when channels are configured,
  mirroring the channel endpoints' 503 degradation) and reports it alongside
  `enabled` in `/api/v1/ui/config`. Writing an `available:` key in this file is
  a **validation error** (`schemas/ui.schema.json`, `additionalProperties:false`)
  — `make validate` rejects it. The console renders a panel only when it is
  **both** enabled here **and** available at runtime.

An absent `config/ui.yaml` is the expected zero-config case (Slice-1 defaults
apply); a malformed file is logged at WARN and soft-degrades to defaults, so a
config typo never blocks the console from booting (the `channels.yaml`-consistent
posture).

---

## Security — do not expose beyond localhost

**The entire REST surface is unauthenticated until
[RFC 0039](../rfcs/0039-user-accounts-authentication.md) ships.** The console
does not add authentication; it makes that surface *more discoverable and
usable from a browser*, which raises the stakes of accidental exposure.

The mitigations the console ships with:

- **`--enable-ui` defaults off.** You opt in explicitly.
- **The orchestrator binds `127.0.0.1` by default** (`--http-bind 127.0.0.1`).
- **The console is read-mostly.** Slice 1's writes are chat, the optional channel
  publish, and — only when you opt in — [group-channel creation](#creating-a-channel-opt-in),
  all against existing endpoints. Channel creation is a deliberate, signed-off
  **structural-write-before-auth** carve-out: it adds **zero new reachability**
  (the `POST /api/v1/channels` endpoint is already exposed unauthenticated, so the
  console changes *discoverability*, not *reachability*), it is **off by default**,
  and `create.available` becomes capability-gated once RFC 0039 auth lands. The
  destructive / admin control plane is Slice 5 and is **hard-gated on RFC 0039
  auth** — it cannot be enabled before auth exists.

**The rule:** exposing the console (or the orchestrator's `:8080` REST surface
at all) **beyond localhost requires fronting the orchestrator with an
authenticating reverse proxy** until RFC 0039 lands. Do not bind the
orchestrator to `0.0.0.0`, and do not publish `:8080` on a routable interface,
without that proxy.

**CSRF posture (forward-looking).** Slice 1 introduces browser-issued `POST`s
(chat, optional channel publish) to a surface that today has no auth and no
CSRF defense. While the surface is unauthenticated this is moot. But the moment
the recommended exposure path lands — a fronting proxy adding *cookie/session*
auth — those same-origin `POST`s become CSRF targets. The auth layer
(RFC 0039 / the proxy) must pair cookie auth with a CSRF mitigation (SameSite
cookies, a CSRF token, or a custom-header/Origin check); the console's API
client is built to send whatever token/header that layer chooses. No live
mitigation ships until auth exists — it is flagged here because the console is
what first makes these endpoints reachable from a browser.

---

## What is not in Slice 1

Deferred by RFC decision (2026-06-02); each is its own later slice
([RFC §E](../rfcs/0048-operator-tester-web-console.md#e-later-slices-roadmap)):

| Not in Slice 1 | Where it lands |
|----------------|----------------|
| **Memory inspector / memory strip** (read-only four-tier views) | Slice 2 (v0.4.0+) — needs a new gRPC read method into the Python `agents/memory/` tiers; `memory_strip` toggle ships off |
| **Isolation verifier** (side-by-side session/epoch recall) | Slice 3 (v0.4.0+) — depends on Slice 2 |
| **Cost & observability** (cost summary + live log stream) | Slice 4 (v0.4.0+) — `cost` toggle ships off |
| **Control plane** (workflow runs, admin actions) | Slice 5 — hard-gated on RFC 0039 auth |
| **Channel real-time push** (OQ4) / **chat token streaming** (OQ5) | Named later enhancements; Slice 1 polls and uses synchronous chat |
| **Authentication / multi-tenancy** | RFC 0039 territory; Slice 1 only commits to *not blocking* it |

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `/ui/` returns 404 | `--enable-ui` is off (the default), or you reached a non-existent path. Run with `--enable-ui` (or `make run-ui`). |
| The console shows a "run `make ui`" placeholder | The binary was built without the real bundle (`go build` / `make build-orchestrator` alone embeds the placeholder). Run `make ui` first, or use `make run-ui` / the Docker image. |
| Every asset 404s under `/ui/` | A bundle built without Vite's `base: "/ui/"`. Use `make ui` (configured correctly); do not hand-build. |
| The Channel-timeline panel is missing | `channel_timeline.available` is false — channels are not wired. Check the channel config; the panel hides itself when its subsystem is absent. |
| The **New channel** button is missing | The create affordance needs **both** `channel_timeline.create_enabled: true` (you opted in) **and** `create.available: true` (the channel store is wired). Confirm with `curl -s localhost:8080/api/v1/ui/config \| jq '.panels.channel_timeline.create'`. See [Creating a channel](#creating-a-channel-opt-in). |
| Creating a channel fails with a conflict | A `group:<name>` with that name already exists (`409`). Pick a different name; the form keeps your entries so you can retry. |
| `make validate` fails on `config/ui.yaml` | You likely added an `available:` key (runtime-derived, not authored) or a malformed panel entry. See [§ feature-toggle model](#the-feature-toggle-model-configuiyaml). |

---

## Related documentation

- [RFC 0048 — Operator & Tester Web Console](../rfcs/0048-operator-tester-web-console.md) — canonical spec.
- [RFC 0048 Phase 1 PR plan](../rfcs/0048-phase1-pr-plan.md) — the six-PR implementation workstream.
- [MT-CONSOLE-001](../manual-tests/MT-CONSOLE-001.md) — fresh-stack manual test for the console.
- [Sessions guide](sessions.md) / [Epochs guide](epochs.md) — the isolation axes the Chat panel's selectors pass through.
- [Channels guide](channels.md) — the channel fan-out the timeline panel renders.
- [Persona agents guide](persona-agents.md) — the personas you chat with.
