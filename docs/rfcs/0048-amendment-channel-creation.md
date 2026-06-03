# RFC 0048 Amendment — Operator/Tester Channel Creation (Web Console)

**Type**: amendment to [RFC 0048](0048-operator-tester-web-console.md) §D (Slice 1 — Live Interactions), §C (Feature-Toggle Model), §E (Later Slices — relationship to Slice 5 Control Plane), and §F (Auth/Multi-Tenancy forward-compat)
**Status**: 📋 Proposed (one carve-out requires sign-off — see §D and [Open Questions](#open-questions))
**Date**: 2026-06-03
**Trigger**: Operator/tester walk-through of the shipped Slice 1 console: a tester setting up a multi-agent scenario (the console's stated audience — *watch personas interact*) cannot create a group channel from the browser. They must either hand-edit [`config/channels.yaml`](../../config/channels.yaml) and restart the orchestrator, or leave the console for the CLI / a raw `POST`. The "watch personas interact" half of Slice 1 ([RFC 0048 §D](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero)) presupposes a channel exists, but the console offers no path to make one.
**Supersedes**: nothing. It introduces the console's first **structural write** (channel creation) — a deliberate, scoped exception to [RFC 0048's Slice-1-is-read-and-interact Non-Goal](0048-operator-tester-web-console.md#non-goals) and [§F rule 5](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility) ("write slices are auth-gated by construction"). The exception is documented as a local-mode-only carve-out (§D), ships dark behind a new toggle (§A), and **adds no backend endpoint** — it surfaces the `POST /api/v1/channels` handler that already exists ([`channel_handlers.go`](../../internal/server/channel_handlers.go) `handleCreateChannel`).

> **Note on section references.** This document's own sections are lettered §A–§E. References to the base RFC are always written with the `RFC 0048` prefix (e.g. "RFC 0048 §F"). An unqualified "§D" means *this amendment's* §D.

---

## Context

Slice 1 shipped two panels — Chat and Channel Timeline — as a deliberately **read-and-interact** surface ([RFC 0048 Non-Goals](0048-operator-tester-web-console.md#non-goals): *"Slice 1 is read-and-interact, not a full control plane. Creating workflows, registering agents, and destructive admin actions are later slices, gated behind auth."*). The Channel Timeline panel ([`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte)) lists channels, renders history, polls for updates, and lets a human *publish into an existing channel*. Its empty state already tells the operator the truth: *"Chat with a persona to start a DM, or define group channels in `config/channels.yaml`, then re-check."*

That empty state is the gap. The two ways to get a **group** channel today are:

1. **Config-declared** — add an entry to [`config/channels.yaml`](../../config/channels.yaml) and restart the orchestrator. Members cannot change without a reboot.
2. **REST** — `POST /api/v1/channels` ([`handleCreateChannel`](../../internal/server/channel_handlers.go)), which already exists: it takes a `name`, optional `description`, and a non-empty `members` array (each `{ id, respond? }`), derives the canonical id `group:<name>` server-side, and creates the channel + memberships atomically via `CreateChannelWithMembers`, returning `201` with the created channel.

Neither is reachable from the console. For the tester the console exists to serve — *spin up a channel, drop two personas in it, watch them interact* — this forces a context switch out of the browser, which is exactly the first-contact friction [RFC 0048's Motivation](0048-operator-tester-web-console.md#motivation) exists to remove.

This amendment surfaces the **existing** create endpoint in the Channel Timeline panel, behind a new dark toggle, as the console's first structural write. It is **not** the Slice 5 Control Plane — it is a single, narrow, already-exposed mutation, scoped and reconciled with the auth-forward-compat rules below. (DMs and threads are explicitly *not* in scope: both are created implicitly on first message per [RFC 0011](0011-channels-bridges.md) — a chat *is* a DM channel per the [chat-as-DM amendment](0011-amendment-chat-as-dm.md) — so there is nothing to "create" for those types.)

## Why this is not a new slice

A **slice** ([RFC 0048 §A](0048-operator-tester-web-console.md#a-vocabulary)) is *"a vertical, independently shippable bundle of one or more panels plus backend glue."* Channel creation is neither a panel nor a capability area — it is one write action whose natural home is the panel that already *reads* channels. Wrapping a single mutation in its own slice would over-weight it and split the channel experience across two panels (one to create, one to watch). The right unit is a **capability toggle on the existing Channel Timeline panel** (§A), not a new slice. The Slice 5 Control Plane remains the home for the *broad* write surface (workflow runs, agent registration, destructive admin); this amendment carves out the one structural write that is already exposed on the REST surface and is load-bearing for Slice 1's own reason to exist.

## The amended contract

### §A. A `create` capability toggle on the Channel Timeline panel (extends RFC 0048 §C)

Channel creation ships **dark** behind a new per-panel capability flag, consistent with the [RFC 0048 §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model) toggle model (ship dark, operator opts in, render only when both enabled *and* available).

**`config/ui.yaml`** — the `channel_timeline` panel gains one nested operator knob:

```yaml
channel_timeline:
  enabled: true          # the panel itself (Slice 1, unchanged)
  create_enabled: false  # NEW — channel creation affordance; ships dark
```

- `create_enabled` is the **only new authored key**. Default **`false`** — creation is off until an operator opts in, exactly as Slices 2/4 ship off.
- It is additive and namespaced under the panel it extends; the schema ([`schemas/ui.schema.json`](../../schemas/ui.schema.json)) gains `create_enabled` (boolean, default `false`) under `channel_timeline`, keeping `additionalProperties: false`.
- As with `available`, **`create_available` is never authored** — it is runtime-derived (§D) and reported by the server.

**`GET /api/v1/ui/config`** — the `channel_timeline` panel entry carries the capability as a nested object, mirroring the existing `{ enabled, available }` shape:

```json
{
  "panels": {
    "channel_timeline": {
      "enabled": true,
      "available": true,
      "create": { "enabled": false, "available": true }
    }
  }
}
```

- `create.enabled` echoes `create_enabled` from `config/ui.yaml`.
- `create.available` is **runtime-derived** by the server (§D): the channel store must be wired, and the identity posture must permit creation. The console renders the create affordance only when `create.enabled && create.available`, exactly as it renders a panel only when `enabled && available`.
- Older clients that do not know the `create` key ignore it and show no create affordance — the existing graceful-degradation contract ([RFC 0048 §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model)) holds with no change.

### §B. Client behaviour — a create affordance in the Channel Timeline panel (client-only)

**No API change** — this is a render-over-existing-API addition, the same posture as the rest of Slice 1.

- When `create.enabled && create.available`, the channel picker row ([`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte), beside the existing **Refresh** control) gains a **"New channel"** affordance that opens a small form:
  - **Name** (required) — the only field the server requires. The client must **not** prepend `group:`; the server derives the canonical `group:<name>` id ([`handleCreateChannel`](../../internal/server/channel_handlers.go)), and a client-side prefix would produce `group:group:<name>`. Surface the resulting id read-only so the operator sees what will be created.
  - **Description** (optional) — passed through verbatim.
  - **Members** (at least one required — the endpoint rejects an empty `members` array with `400`) — a multi-select populated from `GET /api/v1/agents` (the panel already loads this list for sender decoration, [`agentsById`](../../web/src/panels/ChannelTimeline.svelte)), each with a per-member **respond policy** select (`when_mentioned` (default) / `always` / `never`, matching [RFC 0011 §D](0011-channels-bridges.md) `RespondPolicy`).
- On submit, `POST /api/v1/channels` with `{ name, description?, members: [{ id, respond }] }`. On `201`, **reuse `loadChannels()`** to refresh the picker and select the newly-created channel (`group:<name>`) — the existing `loadChannels` already supports a one-shot "select this channel" hand-off via `nav.targetChannel` ([`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte)); the create flow sets it to the new id and reloads, landing the operator directly in the channel they just made.
- **Error rendering rides the existing envelope.** The endpoint already returns the project's error shape: `400` (missing name / empty members), `409 CONFLICT` (a `group:<name>` already exists), `503` (store unwired). The form surfaces `err.message` verbatim through the same `ApiError` path the publish box uses, so a duplicate-name retry reads as a clear conflict, not a silent failure.
- The create form is **collapsed by default** (an affordance, not an always-open panel) so it never crowds the newcomer's first-contact view — consistent with how the [slice1-ux amendment §C](0048-amendment-slice1-ux.md) keeps the scope control behind a disclosure.

### §C. Membership is the operator's, scoped to the identity the console already carries (extends RFC 0048 §F)

The creator and the channel's membership must respect the console's single identity source ([RFC 0048 §F rule 1](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)), not invent a second one:

- Member ids come from the **agent list the server returns**, never free-typed, so the console cannot fabricate participants the backend does not know.
- In local mode the acting principal is the `/ui/context`-derived `local` (or the [slice1-ux §E "Acting as"](0048-amendment-slice1-ux.md) override, which already exists for the publish box on this panel). The create call carries no separate identity field today — `handleCreateChannel` does not take a creator — so there is **no new identity surface**; this is purely a forward-compat note that when RFC 0039 plumbs a real principal, the creator is that principal and membership is scoped to what it can see ([RFC 0048 §F rule 2](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)), which is a purely additive change to the same endpoint.

### §D. The carve-out: a structural write before RFC 0039 auth (the one decision needing sign-off)

This is the single genuine product decision in the amendment, and it bends two base-RFC commitments. It must be signed off before implementation, exactly as the [slice1-ux §E](0048-amendment-slice1-ux.md) identity carve-out was.

**What it bends.** [RFC 0048 Non-Goals](0048-operator-tester-web-console.md#non-goals) declares Slice 1 *read-and-interact, not a control plane*, and [§F rule 5](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility) declares *write slices auth-gated by construction* (Slice 5 hard-depends on RFC 0039). Surfacing channel creation is a structural write landing before RFC 0039.

**Why the carve-out is justified, and narrow:**

1. **Zero new attack surface.** `POST /api/v1/channels` is **already exposed unauthenticated** on the REST surface ([RFC 0048 §Security](0048-operator-tester-web-console.md#security-considerations): the whole surface is unauthenticated until RFC 0039). Anyone who can reach the orchestrator can already create a channel with one `curl`. Surfacing it in the console changes *discoverability*, not *reachability* — the same property [RFC 0048 §Security](0048-operator-tester-web-console.md#security-considerations) already weighs for the console as a whole, and mitigates the same way (`--enable-ui` off by default, `127.0.0.1` bind, fronting proxy required for non-local exposure).
2. **It is one narrow, non-destructive, structural write — not the control plane.** Creating a `group:<name>` channel is bounded (the global `max_channels` cap, default 50, still applies via the store) and non-destructive (no DELETE; channel deletion remains deferred per [`channel_handlers.go`](../../internal/server/channel_handlers.go)). The broad/destructive write surface (workflow runs, agent registration, deletes) stays in Slice 5 behind RFC 0039. This carve-out does not move that line for anything else.
3. **It is load-bearing for Slice 1's own thesis.** "Watch personas interact" requires a channel with personas in it. Without browser-side creation, the console cannot deliver its own hero scenario without a CLI detour.

**Guardrails (normative for the implementing PR):**

- The affordance is **off by default** (`create_enabled: false`) and **gated by both** the toggle and runtime availability (§A). An operator must consciously enable it.
- **`create.available` becomes the forward-compat hook.** Today it is `channelStore != nil`. Once `/ui/context` reports `authenticated: true` (RFC 0039), `create.available` must be driven by a **capability hint** — creation is offered only to a principal whose capabilities include channel creation, never open to any authenticated browser session. This is what keeps §D a *local-mode carve-out* rather than a permanent hole in the future auth model: pre-auth it rides the toggle on the unauthenticated localhost surface; post-auth it becomes capability-gated, and the toggle alone can never re-open it to an unprivileged principal.
- **No new endpoint, no new privileged surface.** The console only calls the create endpoint that already exists, with the identity it already carries — exactly what a CLI caller does today. The new `/ui/config` `create` flags are read-only and ride the existing middleware.
- **CSRF posture is inherited, and flagged.** Like the chat/publish `POST`s [RFC 0048 §Security](0048-operator-tester-web-console.md#security-considerations) already calls out, this browser-issued `POST` becomes a CSRF target the moment a fronting proxy adds cookie/session auth; the create call must send whatever CSRF mitigation the auth layer chooses, on the same footing as the existing writes.

## What this amendment does NOT change

- **It does not add the Slice 5 Control Plane.** This is one structural write (group-channel creation), not workflow/agent/session management or any destructive action. Slice 5 and its RFC 0039 hard-gate are untouched.
- **It does not add a backend endpoint, store method, or schema migration.** `POST /api/v1/channels` and `CreateChannelWithMembers` already exist and are unchanged. The only backend deltas are: the `create_enabled` knob in `config/ui.yaml` + `schemas/ui.schema.json`, and the `create` object in the `/api/v1/ui/config` payload computed by [`ui_handlers.go`](../../internal/server/ui_handlers.go).
- **It does not add DM or thread creation.** Both are implicit-on-first-message ([RFC 0011](0011-channels-bridges.md)); only `group:` channels are explicitly creatable, matching `handleCreateChannel`.
- **It does not add channel deletion or membership editing.** Channel DELETE remains deferred; post-create membership changes (`POST /api/v1/channels/{id}/members` exists but is unwired in the console) are out of scope here.
- **It does not add auth or multi-tenancy.** §D is a scoped local-mode carve-out that becomes capability-gated under RFC 0039; it does not ship auth itself.
- **It does not touch the Chat panel.** Whether the console keeps a Chat panel at all is a separate question handled outside this amendment (see [Related](#related-documentation) — RFC 0032 / chat-façade fate).

## Implementation

Single shippable PR (the backend deltas are thin; the bulk is the Svelte form):

1. **PR — Console channel creation (§A–§D).**
   - Backend: add `create_enabled` to `config/ui.yaml` (default `false`) + `schemas/ui.schema.json`; compute and return the `channel_timeline.create` `{ enabled, available }` object in [`ui_handlers.go`](../../internal/server/ui_handlers.go) (`create.available = channelStore != nil` today; structured for the RFC 0039 capability hint per §D). No change to `handleCreateChannel`. Handler/unit test for the new config shape and availability derivation, per the [RFC 0048 test strategy](0048-operator-tester-web-console.md#test-strategy).
   - Client: a collapsed **"New channel"** form in [`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte) (name → read-only `group:<name>` preview, optional description, member multi-select over `GET /api/v1/agents` with per-member respond policy); `POST /api/v1/channels`; on `201` reuse `loadChannels()` + `nav.targetChannel` to select the new channel; surface the server error envelope (esp. `409` duplicate). Gated on `create.enabled && create.available`. Exercised by the Svelte component tests already established for the panel.

PR D's [slice1-ux §E](0048-amendment-slice1-ux.md) "Acting as" override already lives on this panel's publish box; the create form inherits the same acting principal with no extra work.

## Open Questions

1. **The §D carve-out — ship a structural write before RFC 0039?** Proposed: **yes**, behind a default-off toggle, justified because the endpoint is already exposed unauthenticated (zero new reachability) and creation is load-bearing for Slice 1's "watch personas interact" thesis; reconciled by making `create.available` capability-gated once auth lands. This is the one decision that needs sign-off (mirrors slice1-ux §E). *Alternative:* hold channel creation for Slice 5 and accept the CLI/`config/channels.yaml` detour until RFC 0039 — honours the Non-Goal literally at the cost of the console's own hero scenario.
2. **Toggle shape — nested `create_enabled` on `channel_timeline`, or a flat top-level panel key?** Proposed: **nested**, because creation is a capability *of* the timeline panel, not a panel of its own (see [Why this is not a new slice](#why-this-is-not-a-new-slice)).
3. **Member-policy default in the form — `when_mentioned`?** Proposed: **yes**, matching the server default in `handleCreateChannel` (an empty `respond` falls through to `RespondWhenMentioned`), so the form's default and the API's default never diverge.

## Related documentation

- [RFC 0048 — Operator & Tester Web Console](0048-operator-tester-web-console.md) — the amended spec (§C toggles, §D Slice 1, §E later slices / Control Plane, §F forward-compat, §Security).
- [RFC 0048 amendment — Slice 1 Interaction-UX Hardening](0048-amendment-slice1-ux.md) — the sibling amendment; §E's "Acting as" override is the identity affordance this amendment's create form inherits.
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) — the channel model (`group`/`dm`/`thread`), `RespondPolicy`, and the store this creation writes to.
- [RFC 0011 amendment — Chat as DM](0011-amendment-chat-as-dm.md) — why DMs/threads are implicit and not in this amendment's create scope.
- [RFC 0032 — Wire-Level Channel Interaction Layer and Chat-Façade Unification](0032-channel-interaction-layer.md) — owns the separate question of the Chat surface's fate (its OQ 1).
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md) — the auth layer the §D carve-out becomes capability-gated under.
