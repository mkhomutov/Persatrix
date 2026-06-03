# RFC 0048 Amendment — Retire the Chat Panel; Consolidate on the Channels Panel

**Type**: amendment to [RFC 0048](0048-operator-tester-web-console.md) §A (Vocabulary — panel set), §C (Feature-Toggle Model — `chat` toggle), and §D (Slice 1 — the two-panel structure)
**Status**: ✅ Accepted & Implemented (2026-06-03 — both PRs landed; the Chat panel is fully retired, see [Implementation](#implementation))
**Date**: 2026-06-03
**Trigger**: A chat *is* a `dm:` channel server-side ([chat-as-DM amendment](0011-amendment-chat-as-dm.md): `GetOrCreateDM(user_id, agent_id)`). The console therefore ships **two panels for one underlying model** — Chat (a DM with one persona) and Channel Timeline (every channel, DMs included). The [slice1-ux amendment](0048-amendment-slice1-ux.md) already documented the cost of that split (Context #7: *"the two hero panels never connect"*) and patched it with a one-way deep-link (§F). The structural fix is to stop maintaining two surfaces over one model: retire the Chat panel and let the Channels panel host both kinds of conversation.
**Supersedes**: the two-panel framing of [RFC 0048 §D](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero) (Chat + Channel Timeline as separate panels). It does **not** remove any backend surface — the chat REST/gRPC façade (`POST /api/v1/agents/{id}/chat`, `GET /api/v1/agents/{id}/chat/history`, `AgentService.SendChatMessage`) is untouched here; its fate is owned by [RFC 0032](0032-channel-interaction-layer.md) (OQ 1). This amendment retires the **console panel only**.

> **Note on section references.** This document's own sections are lettered §A–§D. References to the base RFC are written with the `RFC 0048` prefix. An unqualified "§B" means *this amendment's* §B.

## Context

[RFC 0048 §D](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero) shipped Slice 1 as two panels:

- **Chat** (`Chat.svelte`) — pick a persona, talk to it over the synchronous chat API, with a persona header (name — role — capabilities), session/epoch scope selectors, an abortable turn, transcript seeded from persisted history, and an "Acting as" identity override.
- **Channel Timeline** ([`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte)) — pick a channel, watch its history, poll for updates, publish into it.

But the [chat-as-DM amendment](0011-amendment-chat-as-dm.md) established that a chat is **not a separate transport** — it is a `dm:` channel, persisted in the same `channels` + `messages` store the timeline already reads, with the chat endpoint a synchronous-reply *façade* over it. The two panels are two views of one model. The slice1-ux amendment already felt this seam: its Context #7 names the silo as a defect and its §F bolts on a "view this conversation in the timeline" deep-link — a workaround for two panels that should be one. Subsequent fixes (the persona switcher and sticky cross-tab selection, [#512](https://github.com/mkhomutov/Persatrix/pull/512)/[#513](https://github.com/mkhomutov/Persatrix/pull/513)) are maintenance on a duplicated surface.

The clean state is **one conversation panel**. The Channels panel already lists every channel (DMs included), renders history, polls, publishes, and (per the [channel-creation amendment](0048-amendment-channel-creation.md)) creates group channels. The only thing it lacks is the Chat panel's *DM-with-a-persona ergonomics*. This amendment folds those in and removes the Chat panel, so the console maintains one surface over the one model.

## What this is and is not

- **It is** a console-UX consolidation: retire the Chat *panel*, absorb its DM ergonomics into the Channels panel. Client-side plus the `chat` toggle removal.
- **It is not** removal of the chat backend façade. `POST /api/v1/agents/{id}/chat` and `GET /api/v1/agents/{id}/chat/history` (slice1-ux §B) **stay** — the consolidated panel uses them under the hood as the DM open/seed/synchronous-reply path. Whether the façade itself is ever deprecated is [RFC 0032](0032-channel-interaction-layer.md)'s OQ 1, sequenced to follow this panel retirement (this amendment removes the façade's in-tree *browser* consumer, leaving only the CLI and out-of-tree callers — which is exactly the precondition RFC 0032 §D's 3b branch needs).

This is the deliberate seam the console-now/0032-later decision draws: **panel gone now (v0.3.x), façade fate decided later (RFC 0032, v0.4.0+).**

## The amended contract

### §A. Panel set: `chat` is removed; Channels is the single conversation panel (amends RFC 0048 §A/§C)

- [`config/ui.yaml`](../../config/ui.yaml) drops the `chat:` entry. The schema ([`schemas/ui.schema.json`](../../schemas/ui.schema.json)) needs **no change** — `panels` already accepts arbitrary panel names (`additionalProperties` → `panel`), so removal is purely deleting the authored entry (and any `chat:` override under `config/environments/*`).
- The client allow-list `KNOWN_PANELS` ([`bootstrap.js`](../../web/src/lib/bootstrap.js)) drops the `chat` descriptor and its `#/chat` route. Per the existing [RFC 0048 §C](0048-operator-tester-web-console.md#c-the-feature-toggle-model) unknown-panel rule, a stale `chat:` still present in a deployment's `ui.yaml` is simply ignored by a client that no longer knows the panel — graceful degradation holds, so the rollout is not order-sensitive.
- The Channels panel (`channel_timeline`) stays enabled and becomes the console's single conversation surface, hosting both **DMs** (talk to a persona) and **group channels** (watch personas interact). Renaming the panel label from "Channels" to "Conversations" is a reasonable cosmetic follow-up but is **not** required by this amendment.

### §B. The Channels panel absorbs the Chat panel's DM ergonomics (client-only)

**No API change.** Everything below already exists as endpoints or as components in `Chat.svelte`; this is a re-host, not new capability. Nothing the Chat panel did is lost:

- **Start/open a DM with a persona.** The panel gains a persona entry point — the existing `PersonaPicker` filtered by `isChattable` ([`agents.js`](../../web/src/lib/agents.js)) — that resolves the persona's DM channel and selects it in the timeline. Resolution reuses slice1-ux §B's read-only `LookupDM` (via `GET /api/v1/agents/{id}/chat/history`, which returns the resolved DM channel id and seeds history); a never-messaged persona resolves to an empty conversation that the first send opens (`GetOrCreateDM`, server-side, unchanged). This is the same machinery slice1-ux §F already uses for the deep-link — now it is the panel's own entry point rather than a cross-panel hand-off.
- **Persona legibility in DM mode.** The `PersonaHeader` (name — role — capabilities, slice1-ux §A) renders above a selected DM, so a DM is not an anonymous channel row.
- **Synchronous send, preserved.** In DM mode the composer sends via the chat façade (`sendChat`), keeping the synchronous "thinking…" round-trip and abortable turn (`Chat.svelte` `chatController`) the Chat panel had; the timeline poll keeps any follow-on agent traffic live. Group-channel mode keeps publish-and-poll (`publishMessage`) as today. (When RFC 0032 resolves the façade's fate, DM-mode send migrates to channel publish-and-await with no further console-panel change.)
- **Scope selectors and identity override, preserved.** The `ScopeSelector` (session/epoch, slice1-ux §C) applies to DM-mode turns so the isolation-demo story RFC 0048 §D calls out survives the consolidation; the "Acting as" override (slice1-ux §E) already lives on this panel's publish box and now covers DM sends too.
- **History continuity, preserved.** Resumed DM history comes from the channel load the panel already performs.
- **Sticky selection.** The cross-mount persona/channel selection ([`selection.svelte.js`](../../web/src/lib/selection.svelte.js), [#512](https://github.com/mkhomutov/Persatrix/pull/512)/[#513](https://github.com/mkhomutov/Persatrix/pull/513)) is rehomed into the single panel; with one panel there is no cross-tab persona handoff to keep in sync, which is a net simplification.

**Rendering decision (locked 2026-06-03, PR 1).** A DM **renders as a channel**: the consolidated panel reuses the existing `ChannelMessage` timeline rows + poll loop for DM messages, rather than porting the Chat panel's bespoke `ChatMessage` transcript. A chat *is* a `dm:` channel, so the persisted channel messages are the single source of truth — a `sendChat` turn surfaces via the next head poll (no local echo, so no double-show). One consequence: the **per-turn scope annotation** of slice1-ux §B caveat 2 (the "session: … / epoch: …" line under each live turn) is **dropped** — store messages don't record the per-turn override. The `ScopeSelector` itself is unchanged and still scopes each DM send; only the per-message label goes away, in exchange for one rendering path across both conversation kinds. (Considered and rejected: keeping the `ChatMessage` transcript with a parallel poll — it reintroduces the echo/poll reconciliation the channel model avoids, for a label of marginal value in a 1:1 DM.)

### §C. The cross-panel deep-link collapses into in-panel selection (amends slice1-ux §F)

slice1-ux §F's "view this conversation in the timeline" deep-link and the `nav.targetChannel` intent (the former `web/src/lib/nav.svelte.js`, deleted by PR 1) existed solely to bridge two panels. With one panel, "view this DM as a channel" is just selecting it — already the panel's native action. `nav.svelte.js` and the deep-link affordance are **removed**; slice1-ux §F's *intent* (a chat is a watchable channel) is satisfied more directly, by construction, because the DM and the channel are the same selection in the same panel.

### §D. Empty states and onboarding merge (amends slice1-ux §F)

The two panels' onboarding empty states (slice1-ux §F) merge into one. "No personas registered" and "No channels exist" become a single first-contact surface on the consolidated panel: pick a persona to start a DM, or pick/create a group channel — each still linking the [web-console quick-start](../guides/web-console.md). One panel means one dead-end-free entry point instead of two.

## What this amendment does NOT change

- **No backend removal.** The chat REST/gRPC façade and `chat/history` endpoint are untouched and remain the DM open/seed/synchronous-reply path. RFC 0032 owns whether they are ever deprecated.
- **No new endpoint, store method, or schema migration.** Schema is unchanged (§A); the only config delta is deleting the `chat:` toggle entry.
- **No change to identity, auth, or multi-tenancy posture.** The single-identity-source rule and the slice1-ux §E "Acting as" carve-out carry over verbatim onto the consolidated panel.
- **Composes with the channel-creation amendment.** The same Channels panel gains DM ergonomics (here) and **group**-channel creation (the [channel-creation amendment](0048-amendment-channel-creation.md)). One reconciliation: the create form's earlier **"Direct message" mode** is a *second* DM entry point on top of this amendment's persona picker, so it is **removed** (the create form is group-only now); the persona picker is the single DM affordance, matching the channel-creation amendment's "group-channel creation" framing. The create form's post-create landing also drops the removed `nav.targetChannel` for the in-panel `pendingSelectId` hand-off (§C).

## Implementation

Depends on the slice1-ux amendment (§A persona legibility, §B chat-history/`LookupDM`, §C scope selector, §E "Acting as", §F resolved-DM-id) being shipped — all are reused, not rebuilt.

1. **Consolidate conversations onto the Channels panel (§B–§D). ✅ Done.** Client: added the persona entry point + DM mode to [`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte) (persona picker, persona header, synchronous `sendChat` in DM mode, scope selector, abortable turn) — DMs **render as channels** (`ChannelMessage` rows + poll, per the rendering decision in §B), with the chat composer extracted to [`DmComposer.svelte`](../../web/src/panels/DmComposer.svelte) and the shared timeline+poll to [`ConversationFeed.svelte`](../../web/src/panels/ConversationFeed.svelte); merged the onboarding empty states; rehomed the sticky DM selection (`selection.dmAgent`); deleted `nav.svelte.js` and the §F deep-link. Reused `PersonaPicker`/`PersonaHeader`/`ScopeSelector` and the existing `ChannelMessage` timeline. The create form was reverted to **group-only** (its redundant "Direct message" mode removed — see §"Composes…").
2. **Remove the Chat panel (§A). ✅ Done.** Deleted `Chat.svelte` + `ChatMessage.svelte`, dropped the `chat` entry from `KNOWN_PANELS` ([`bootstrap.js`](../../web/src/lib/bootstrap.js)), the `App.svelte` `COMPONENTS` map, `config/ui.yaml`, the Go default config + availability switch ([`ui_config.go`](../../internal/server/ui_config.go) / [`ui_handlers.go`](../../internal/server/ui_handlers.go)); retired the now-orphaned `selection.chatAgent` + `pickInitialAgent`. Slice 1 is now a **single-panel** console (`channel_timeline`); the shell keeps its multi-panel tab scaffold for the v0.4.0+ panels. The Go config/handler tests + the Svelte shell tests assert the panel set no longer includes `chat`; the consolidated panel covers the DM flow E2E.

Both PRs were folded into a single change (the consolidated panel was proven green before the Chat panel was removed, so the DM flow never regressed). The standalone Chat panel is gone from the UI.

## Open Questions

1. **Panel rename.** Relabel `channel_timeline` from "Channels" to "Conversations" to reflect that it now hosts DMs + group channels? **Resolved (2026-06-03): no for now** — cosmetic follow-up, not blocking; the panel keeps its "Channels" heading and the `channel_timeline` *name* (config/route/key) stays for compatibility regardless. Revisit when the Chat panel is removed (PR 2), when "Conversations" reads truest.
2. **DM-mode send transport.** Keep synchronous `sendChat` for DM mode now, or switch DM send to channel publish-and-poll immediately to drop the façade dependency ahead of RFC 0032? **Resolved (2026-06-03): keep `sendChat`** — it preserves the synchronous "thinking…" ergonomics and the migration to publish-and-await is RFC 0032's to sequence; doing it here would pre-empt 0032's OQ 1 with no console-visible benefit.

Resolved during implementation (PR 1): a DM **renders as a channel** (`ChannelMessage` + poll), dropping the per-turn scope *annotation* — see the [§B rendering decision](#b-the-channels-panel-absorbs-the-chat-panels-dm-ergonomics-client-only).

## Related documentation

- [RFC 0048 — Operator & Tester Web Console](0048-operator-tester-web-console.md) — the amended spec (§A panel set, §C toggles, §D Slice 1).
- [RFC 0048 amendment — Slice 1 Interaction-UX Hardening](0048-amendment-slice1-ux.md) — supplies the machinery (§A/§B/§C/§E/§F) the consolidated panel reuses; this amendment collapses its §F deep-link.
- [RFC 0048 amendment — Operator/Tester Channel Creation](0048-amendment-channel-creation.md) — the sibling write affordance on the same panel.
- [RFC 0011 amendment — Chat as DM](0011-amendment-chat-as-dm.md) — why a chat is a `dm:` channel, which is what makes this consolidation lossless.
- [RFC 0032 — Wire-Level Channel Interaction Layer and Chat-Façade Unification](0032-channel-interaction-layer.md) — owns the backend chat-façade fate (OQ 1); this panel retirement is its sequencing precondition.
