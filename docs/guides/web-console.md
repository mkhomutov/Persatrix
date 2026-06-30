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
- [The conversation panel](#the-conversation-panel)
  - [Direct-message a persona](#direct-message-a-persona)
  - [Watch a group channel](#watch-a-group-channel)
- [Creating a channel](#creating-a-channel)
- [Channel settings — edit governance from the browser](#channel-settings--edit-governance-from-the-browser)
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
  the conversation panel derives its `user_id` from this `principal` — it is **never**
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
./bin/persatrix-server --enable-ui        # serve /ui (still localhost-only)
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

## The conversation panel

The console has **one** conversation surface — the **Channels** panel. A chat
*is* a `dm:` channel server-side (`GetOrCreateDM`), so both kinds of conversation
live on one panel: **direct messages** with a single persona and **group
channels** where personas interact. (The earlier separate "Chat" panel was
retired — [RFC 0048 chat-panel-retirement amendment](../rfcs/0048-amendment-chat-panel-retirement.md).)

### Direct-message a persona

The hero moment — talk to a persona over the synchronous chat API:

1. Pick a persona from the **persona picker** at the top of the panel
   (`GET /api/v1/agents`). The conversation opens with a persona header
   (name — role — capabilities); a reload resumes the persisted history.
2. Type a message and send it (`POST /api/v1/agents/{id}/chat` with
   `participant_type:"user"` and the `user_id` derived from `/ui/context`). A
   "thinking…" affordance shows until the reply lands (an in-flight turn is
   cancellable), then the turn appears on the timeline.

**Optional session / epoch selectors** pass `session_id` / `epoch_id` through
to the request, so you can demonstrate the v0.3.5 isolation story from the
browser: switch the [epoch](epochs.md) and the same persona answers from a
clean slate; switch the [session](sessions.md) and it answers from a different
room's memory. Leave them unset for the default room.

Over-length messages are caught client-side (mirroring the server's
4 000-character limit) and the server's error envelope is surfaced as a
user-visible message rather than crashing the panel.

### Watch a group channel

1. Pick a channel from the **channel picker** (`GET /api/v1/channels`; DMs are
   reached through the persona picker, so the channel picker lists group
   channels only).
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

### The interaction-summary affordance (v0.3.8)

When a conversation **closes** — a group brainstorm ends on a Layer 4 end-vote,
trips the Layer 1 cost ceiling, or goes idle — the conversation view renders an
**"interaction closed" affordance** below the live turns, carrying the
[RFC 0020](../rfcs/0020-interaction-lifecycle.md) one-per-interaction **summary**
and the close trigger (*went idle* / *ended* / *cost limit reached*). This is the
"a real result" half of the v0.3.8 *Conversations that converge* story: a
terminated brainstorm hands back a readable synthesis, not just a stop.

It is **additive and self-fetching** — an open interaction's live turns render
exactly as before, and the affordance appears only at close (reading
`GET /api/v1/agents/{id}/interactions/closed`, merged across the channel's
participants). If the on-close summariser failed, the affordance shows an honest
"summary unavailable" state rather than a blank. The same summary is readable
from the terminal via `persatrix agent interactions <agent>` (see
[channels.md §"The interaction-summary surface"](channels.md#the-interaction-summary-surface-rfc-0020--v038)).

---

## Creating a channel

The Channels panel can also **create** a group channel from the browser — so you
can spin one up, drop two personas in it, and watch them interact without leaving
the console for the CLI or hand-editing
[`config/channels.yaml`](../../config/channels.yaml). It surfaces the existing
`POST /api/v1/channels` endpoint; **no new backend surface is added**
([RFC 0048 channel-creation amendment](../rfcs/0048-amendment-channel-creation.md)).

It is **on by default** when the console is running with channels wired. It is a
**structural write before auth**, so read the
[Security](#security--do-not-expose-beyond-localhost) note before exposing the
console beyond localhost. To **hide** the affordance, set `create_enabled: false`
under the `channel_timeline` panel in [`config/ui.yaml`](../../config/ui.yaml):

```yaml
panels:
  channel_timeline:
    enabled: true
    create_enabled: false   # default true — set false to hide channel creation
```

**It renders only when two conditions hold:**

1. **`create_enabled` is on** (the default; the snippet above turns it off).

2. **Channels are wired.** Just like the panel's own `available` flag, the
   create affordance's `create.available` is **runtime-derived** — true only when
   the channel store is wired. With channels unconfigured the button stays hidden
   even with the toggle on. (`create.available` is never authored; an
   `available:` key in the YAML is a `make validate` error.)

**Using it.** In the **Channels** tab, click **New channel** (beside Refresh),
enter a name (the server derives the canonical `group:<name>` id, shown
read-only — do not type the `group:` prefix yourself), an optional description,
and pick members — **only persona agents** are listed, each with a per-member
respond policy (`when_mentioned` (default) / `always` / `never`). Task agents run
workflow steps and never hold a conversation, so they are not selectable. On
success the picker reloads and selects the channel you made.

   **You are added automatically.** The acting user (the `/ui/context` principal)
   is added to the new channel as a member with `respond: never`, because the
   store rejects a publish from a non-member (a poster must be in the channel).
   `never` means you can post but are never dispatched a turn like an agent — so
   you can immediately publish into the channel you just created.

> **Group channels only.** This form creates `group:` channels. To start a **DM**
> with a persona, use the **persona picker** at the top of the panel
> ([Direct-message a persona](#direct-message-a-persona)) — that is the single DM
> entry point. DMs and threads are created implicitly on first message
> ([RFC 0011](../rfcs/0011-channels-bridges.md)), so there is nothing to "create"
> for those.

**Verify the toggle is live:**

```bash
curl -s http://localhost:8080/api/v1/ui/config | jq '.panels.channel_timeline'
# want: { "enabled": true, "available": true,
#         "create": { "enabled": true, "available": true } }
```

Both `create.enabled` **and** `create.available` must be `true` for the **New
channel** affordance to render — the same `enabled && available` rule every panel
follows.

> **Scope.** Channel **deletion** and post-create membership editing are not in
> Slice 1.

---

## Channel settings — edit governance from the browser

A selected **group channel** can have its governance knobs read and edited from
the console — the browser counterpart to the CLI
[`channel config`](channels.md#editing-governance-config-at-runtime--channel-config-rfc-0050-phase-1)
verb group (RFC 0050 Phase 2). Both surfaces ride the **same**
`GET`/`PATCH /api/v1/channels/{id}/config` endpoint and the **same** per-channel
revision, so a value set in one is what the other reads back — one source of
truth, the store. It is a **Channel settings** disclosure nested in the
**Channels** tab beside the member roster, shown only for a watched **group**
channel (not DMs).

**It ships on.** The schema default is `false`, but the delivered
[`config/ui.yaml`](../../config/ui.yaml) sets `config_edit_enabled: true` (RFC
0050) — set it back to `false` under the `channel_timeline` panel to disable it:

```yaml
panels:
  channel_timeline:
    enabled: true
    config_edit_enabled: true   # shipped on (schema default false) — gates BOTH the web panel and CLI uniformly
```

This is the **same toggle** that gates the CLI `channel config` verbs, covering
the whole `/config` endpoint (read *and* write): on exposes both the panel and
the CLI verbs; off returns `403` to both. The panel renders only
when **both** `config_edit.enabled` and `config_edit.available` are true — the
usual `enabled && available` rule. `available` is **runtime-derived** (true only
when the channel store and router are both wired, mirroring the endpoint's `503`)
and never authored — an `available:` key in the YAML is a `make validate` error.
Verify with:

```bash
curl -s http://localhost:8080/api/v1/ui/config | jq '.panels.channel_timeline.config_edit'
# want: { "enabled": true, "available": true }
```

**Using it.** Each knob shows its effective value and a provenance badge —
**Overridden on this channel** or **Inherited default**. To change one, untick
**Inherit fleet default** and set the value; to revert, re-tick it. **Save
settings** sends only the knobs you touched (a sparse patch), carrying the loaded
revision as an `If-Match` guard:

- A reverted knob sends an explicit "unset → inherit"; an override left blank is
  skipped, not sent as `0` (a no-op save sends nothing).
- The **escalation chair** picker offers only floor-capable members (an observer
  cannot chair); a chair needs `floor_control` on, else the save `400`s (a
  cross-field conflict the picker cannot prevent).
- `interaction_budget_tokens` is **router-wired and live-enforced** (RFC 0050
  amendment), so an inherited value resolves to a concrete number, not empty.
- Since v0.3.11 the panel renders an **Autonomous channel** section (RFC 0052) —
  the `autonomous` knobs (enable, Topic/Goal, Agenda, Convener, Max rounds) on the
  same PATCH, plus (PR 3) a **Convene** action
  ([§13](channels.md#13-autonomous-channels-rfc-0052)).
- On a concurrent edit, the save returns `409`; the panel **reloads the latest
  config and replays your pending edits on top** rather than blind-overwriting,
  and asks you to review and save again.

> **First-edit behavior (✅ ISSUE-0103 resolved 2026-06-15).** Editing one knob on
> a YAML-seeded channel **preserves** its other knobs (including the YAML chair):
> the first edit seeds its merge base from the channel's resolved governance
> ([ISSUE-0103](../issues/ISSUE-0103-first-config-edit-detaches-yaml-seeded-knobs.md)).
> Expect the channel to become **store-canonical** (previously-inherited knobs now
> read source `channel`), and a lone `floor_control: false` on a chaired channel to
> be **rejected** — clear the chair in the same save. Still a governance write
> **before auth** — see
> [Security](#security--do-not-expose-beyond-localhost) before exposing the
> console beyond localhost.

For the live cross-surface acceptance walkthrough, see
[MT-CHANNEL-CONFIG-002](../manual-tests/MT-CHANNEL-CONFIG-002.md).

---

## The feature-toggle model (`config/ui.yaml`)

Panels ship "dark" in [`config/ui.yaml`](../../config/ui.yaml) and are flipped
on per deployment. Slice 1 ships the single consolidated conversation panel on;
later slices ship off so they land additively:

```yaml
panels:
  channel_timeline:
    enabled: true
    create_enabled: true       # default true  — group-channel creation; see "Creating a channel"
    config_edit_enabled: true  # schema default false, shipped on — governance settings panel; see "Channel settings"
  memory_strip:        # Slice 2 (v0.4.0+) — ships off
    enabled: false
  cost:                # Slice 4 (v0.4.0+) — ships off
    enabled: false
```

Two rules make this real:

- **`enabled` is the operator knob** (and `create_enabled` /
  `config_edit_enabled` are per-panel capability knobs alongside it). They decide
  whether the console *offers* a panel or affordance. `config_edit_enabled` has a
  **schema default of `false`** but ships **`true`** in `config/ui.yaml` (RFC
  0050); `create_enabled` ships **true** too.
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
  publish, and [group-channel creation](#creating-a-channel), all against existing
  endpoints. Channel creation is a deliberate, signed-off
  **structural-write-before-auth** carve-out: it adds **zero new reachability**
  (the `POST /api/v1/channels` endpoint is already exposed unauthenticated, so the
  console changes *discoverability*, not *reachability*); it is **on by default**
  when the console is enabled (itself off by default), set `create_enabled: false`
  to hide it, and `create.available` becomes capability-gated once RFC 0039 auth
  lands. The
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
| The **New channel** button is missing | The create affordance needs **both** `channel_timeline.create_enabled: true` (the default — confirm it wasn't set false) **and** `create.available: true` (the channel store is wired). Confirm with `curl -s localhost:8080/api/v1/ui/config \| jq '.panels.channel_timeline.create'`. See [Creating a channel](#creating-a-channel). |
| Creating a channel fails with a conflict | A `group:<name>` with that name already exists (`409`). Pick a different name; the form keeps your entries so you can retry. |
| `make validate` fails on `config/ui.yaml` | You likely added an `available:` key (runtime-derived, not authored) or a malformed panel entry. See [§ feature-toggle model](#the-feature-toggle-model-configuiyaml). |

---

## Related documentation

- [RFC 0048 — Operator & Tester Web Console](../rfcs/0048-operator-tester-web-console.md) — canonical spec.
- [RFC 0048 Phase 1 PR plan](../rfcs/0048-phase1-pr-plan.md) — the six-PR implementation workstream.
- [MT-CONSOLE-001](../manual-tests/MT-CONSOLE-001.md) — fresh-stack manual test for the console.
- [Sessions guide](sessions.md) / [Epochs guide](epochs.md) — the isolation axes the conversation panel's DM scope selectors pass through.
- [Channels guide](channels.md) — the channel fan-out the timeline panel renders.
- [Persona agents guide](persona-agents.md) — the personas you chat with.
