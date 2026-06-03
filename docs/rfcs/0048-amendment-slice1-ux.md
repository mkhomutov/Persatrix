# RFC 0048 Amendment — Slice 1 Interaction-UX Hardening

**Type**: amendment to [RFC 0048](0048-operator-tester-web-console.md) §D (Slice 1 — Live Interactions), §F (Auth/Multi-Tenancy forward-compat — identity), and §G (API Gaps)
**Status**: ✅ Accepted (decisions locked 2026-06-03)
**Date**: 2026-06-03
**Trigger**: Pre-v0.3.6 UX review of the shipped Slice 1 console — the two panels are functionally correct and well-instrumented, but as built they present *"another chat box"*, contradicting [RFC 0048 Goal 4](0048-operator-tester-web-console.md#goals) ("make the differentiator visible — persistence and personality"). The most visible console behaviours read as *stateless* and *faceless*, and — more subtly — the console can *assert* persistence but cannot *show* it, because the one identity axis the persistence story lives on is frozen. All three are fatal for the community-growth thesis the slice exists to serve.
**Supersedes**: nothing — closes experience gaps Slice 1's render-over-existing-API scope left open. All API changes are **additive and backward-compatible**. §E proposes a **scoped, local-mode-only carve-out** to [RFC 0048 §F rule 1](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility) (it does not supersede the rule; the carve-out is documented as testing-only and is inert once RFC 0039 auth lands).

> **Note on section references.** This document's own sections are lettered §A–§F. References to the base RFC are always written with the `RFC 0048` prefix (e.g. "RFC 0048 §F"). An unqualified "§F" means *this amendment's* §F.

---

## Context

Slice 1 ([RFC 0048 §D](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero)) shipped two panels — Chat and Channel Timeline — over today's REST API ([#501](https://github.com/mkhomutov/Persatrix/pull/501), [#502](https://github.com/mkhomutov/Persatrix/pull/502)). A use-pass over the running console — walked as the three users it serves: a **newcomer** (first contact), a **tester** (proving isolation/persistence), and an **operator** — surfaced a cluster of issues that are **not bugs** (the code is race-safe, ARIA-correct, and surfaces the server's error envelope faithfully) but where the *experience* undercuts the slice's reason to exist:

1. **The chat presents as stateless.** The transcript is in-memory only ([`Chat.svelte`](../../web/src/panels/Chat.svelte)), yet the conversation is backed by a **persistent DM channel** server-side (`GetOrCreateDM(user_id, agent_id)`, [`chat_handler.go`](../../internal/server/chat_handler.go)). Reload the tab → blank chat. The persona still remembers, but the UI shows a clean slate, which reads as *"it forgot."* The console's most visible behaviour contradicts the persistence pitch.
2. **The persona is faceless — and the UI is already discarding more than `role`.** The picker shows `name (status)` only. `registry.AgentInfo` carries a `Role` field that the JSON DTO `agentResponse` drops ([`registry.go`](../../internal/registry/registry.go), [`types.go`](../../internal/server/types.go)) — but `agentResponse` *already serves* `capabilities` and `address` ([`agent_handlers.go`](../../internal/server/agent_handlers.go) `agentToResponse`), and the picker ignores those too. "Faceless" is broader than a missing role: the client throws away every persona cue the API hands it but the name.
3. **Isolation can't be driven from the browser.** The session/epoch scope is free-text id entry, even though `GET /api/v1/sessions` already returns **labeled** sessions with a `POST` create path ([`server.go`](../../internal/server/server.go)). The RFC's "quietly demonstrates the v0.3.5 isolation story" requires leaving the browser for the CLI to find a session id.
4. **Conversation ergonomics are missing** — no Enter-to-send (in *either* composer), and the channel timeline renders newest-first (reading a conversation backwards), among smaller polish gaps.
5. **Persistence can be asserted but not *shown* — identity is frozen.** Persistence is keyed on `(user_id, agent_id)`; the console derives `user_id` from `/ui/context` and locks it to the degenerate principal `local` ([`bootstrap.js`](../../web/src/lib/bootstrap.js)), correctly per [RFC 0048 §F rule 1](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility). The consequence is that the headline demo — *"watch it greet me by name; now open as a different user and watch it not know you"* — is impossible from the browser. With a single frozen identity, persistence is invisible *by construction*; the operator sees a chat that merely recalls its own scrollback. Read literally, the forward-compat rule defeats Goal 4 for the one axis persistence lives on.
6. **Empty states are dead ends — in a first-contact tool.** "No personas are registered yet." / "No channels exist yet." / "No messages yet." are full stops with no next step ([`Chat.svelte`](../../web/src/panels/Chat.svelte), [`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte)). The console's stated purpose ([RFC 0048 Motivation](0048-operator-tester-web-console.md#motivation)) is *lowering first-contact friction*; an empty dropdown with no path forward is exactly the friction it exists to remove.
7. **The two hero panels never connect.** Chat ("talk to a persona") and Channel Timeline ("watch personas interact") are fully siloed. The most convincing single demonstration — *chat with a persona, then see that exact conversation persisted as a watchable channel* — is impossible without leaving for the CLI to find the DM channel id, even though the chat DM **is** a channel.

This amendment specifies the **API contract changes** (one additive field, one additive read-only endpoint) and the **client behaviour changes** needed to close 1–7. Client-only changes are listed for completeness but are non-normative — they need no backend agreement, with the single exception of §E (no API change, but a deliberate, sign-off-gated bend of RFC 0048 §F rule 1).

## The amended contract

### §A. Persona legibility on the agent DTO (closes Context #2)

`agentResponse` ([`types.go`](../../internal/server/types.go)) gains one normative field:

```go
type agentResponse struct {
    ID           string   `json:"id"`
    Name         string   `json:"name"`
    Role         string   `json:"role"`          // NEW — from registry.AgentInfo.Role; "" when unset
    Address      string   `json:"address"`       // already served
    Capabilities []string `json:"capabilities"`  // already served
    Status       string   `json:"status"`
}
```

- `role` is the **only new field**. It is populated from `registry.AgentInfo.Role` in `agentToResponse`, and is **additive** — existing clients ignore the new key.
- An empty `role` is valid (not every agent declares one); the client falls back to showing no role rather than a placeholder, exactly as it already falls back `name → id`.
- **`capabilities` and `address` need no API change — they are already populated** by `agentToResponse` ([`agent_handlers.go`](../../internal/server/agent_handlers.go)) and silently dropped by the picker. The client work here is to *render the cues the API already serves*: a persona header above the transcript showing `name — role` and a compact capability list, so the picker stops being a bare `name (status)` dropdown. This is the larger half of "faceless," and it is pure client.
- **`model` is explicitly out of scope.** It is infra detail, not a personality cue, and exposing it on the public agent list is a separate decision (it belongs with the Slice 4 cost panel, if anywhere). This amendment adds `role` only.

No new endpoint — the existing `GET /api/v1/agents` and `GET /api/v1/agents/{id}` simply carry one more field.

### §B. Chat-history continuity endpoint (closes Context #1)

The chat DM is a real, persisted channel, but its id is server-normalised by `GetOrCreateDM` and must **not** be reconstructed client-side (the api.js client already notes DM ids are opaque, colon-bearing keys). To let the Chat panel resume a conversation without leaking DM-id derivation into the client, add a read-only endpoint:

```
GET /api/v1/agents/{id}/chat/history?user_id=<principal>&limit=<n>&before=<rfc3339>
```

- **Returns** the existing `historyResponse` envelope (`{ "messages": [ … ] }`), newest-first, identical in shape to `GET /api/v1/channels/{id}/messages` — so the client reuses its `getChannelHistory` parsing and the messages carry `sender_id`, `content`, `timestamp`, and `metadata` (including `reply_status` for error/empty replies).
- **Resolution is read-only.** It resolves the canonical DM for `(user_id, agent_id)` **without creating it** — a new non-mutating `ChannelStore.LookupDM` (or equivalent) sibling to `GetOrCreateDM`. A persona never chatted with → **`200` with an empty `messages` array**, not `404`: "no history yet" is the expected fresh-start case, not an error.
- `user_id` is **required** (it is half the DM key); `limit`/`before` mirror the channel-history query params ([`channel_query_params.go`](../../internal/server/channel_query_params.go)) and reuse their validation verbatim, so keyset pagination is available for free.
- **Auth/identity posture is unchanged** — `user_id` is the `/ui/context`-derived principal the client already supplies on chat; the endpoint is read-only and rides the same middleware stack as every other `/api/v1/*` route. It exposes nothing the caller could not already read by chatting and re-reading; it just spares them reconstructing the DM id.

**Two implementation caveats — both surfaced by the use-pass, both must be honoured by PR B:**

- **`LookupDM` must preserve `GetOrCreateDM`'s access-control semantics, minus creation.** The chat handler documents `GetOrCreateDM` as *"the access-control checkpoint — DM-membership"* ([`chat_handler.go`](../../internal/server/chat_handler.go)). The read-only sibling must apply the *same* membership/authorization gate it enforces and only drop the create-if-absent half — it must never resolve and return a DM the principal is not a party to. This is moot under today's single-principal localhost mode but becomes load-bearing the moment RFC 0039 plumbs a real principal through; building it in now keeps the later wiring purely additive (RFC 0048 §F rule 2).
- **The flat-list migration must not silently drop the per-turn isolation annotation.** The shipped transcript pins `session`/`epoch` *per turn* and renders a `turn-scope` line ([`Chat.svelte`](../../web/src/panels/Chat.svelte)) — that annotation is the one visible isolation cue the chat panel has today (RFC 0031 / ISSUE-0085). A naive "seed from history → flat message list" loses it, because a persisted message does not necessarily carry the override in its `metadata`. PR B must either (a) reconstruct the annotation from each message's `metadata` where the scope was stamped, or (b) explicitly scope the annotation to *live* (this-session) turns and document that resumed history shows no scope line. Either is acceptable; silently dropping it is a regression dressed as a feature and is not.

**Client behaviour:** on persona-select (and on first mount), the Chat panel fetches this endpoint and seeds the transcript from history, then continues appending live turns. The transcript model shifts from `{prompt, reply}` pairs to a **flat message list** keyed by `sender_id === user_id` (you) vs. otherwise (the persona) — which is both simpler and more correct (it naturally handles the persisted ordering and any non-paired messages). Each seeded message renders its `timestamp` (see §D) — "remembers me from earlier" is unconvincing without a *when*. A reload now resumes the conversation, making persistence *visible* — the point of the slice.

**Alternative considered (rejected):** surfacing `channel_id` on `chatResponse` and having the client fetch `/channels/{channel_id}/messages`. Rejected because it does not solve the **reload-before-first-message** case (no `channel_id` known yet) and couples the client to DM-id semantics. The dedicated agent-scoped endpoint resolves both and keeps DM-id derivation server-side. **Note:** §F (cross-panel continuity) does want the resolved DM channel id *surfaced to the client once known* — that is a deliberate, separate, post-first-message affordance, not a substitute for this endpoint's reload-before-first-message coverage.

### §C. Session selector over the existing sessions API (closes Context #3)

**No API change.** `GET /api/v1/sessions` (labeled list) and `POST /api/v1/sessions` (create with `label`) already exist ([`session_handlers.go`](../../internal/server/session_handlers.go)). The Chat panel's session scope control changes from a free-text id input to a **dropdown populated from `/api/v1/sessions`** (showing `label`, value `id`), with an inline "new session" affordance that `POST`s a label and selects the result. The epoch control stays free-text (no labeled-epoch list endpoint exists; epoch ids are operator-namespace values, consistent with [RFC 0048 §F rule 3](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)). When the session registry is unwired (`503`), the control degrades to the current free-text input rather than disappearing.

The scope control is today buried in a collapsed `<details>` ("Scope (optional)", [`Chat.svelte`](../../web/src/panels/Chat.svelte)). That is the right default for a newcomer (don't overwhelm first contact), but it sits in tension with the RFC's goal of *demonstrating* the isolation story. PR C keeps the disclosure collapsed by default but ensures the active scope (when set) is visible on the turn even when the disclosure is closed — the per-turn `turn-scope` annotation already does this; the dropdown must not regress it.

### §D. Client-only ergonomics (non-normative — no backend agreement needed)

Listed so the amendment's PR scope is complete; none change the API:

- **Enter-to-send in *both* composers** — the chat composer **and** the channel publish box (Enter sends, Shift+Enter inserts a newline). Today both `<textarea>`s swallow Enter and the button is the only send path; fixing only the chat composer would leave the two write surfaces inconsistent, which reads as a bug.
- **Conversational timeline order, with a pinned-scroll guard** — the Channel Timeline renders **oldest-top, newest-bottom** for display (the wire fetch stays `DESC`; the client reverses for render), with the publish box and newest message co-located at the bottom. Autoscroll-to-bottom on a new polled message fires **only when the view is already pinned to the bottom** (within a small epsilon) — never when the operator has scrolled up to read history, which autoscroll would otherwise yank them out of every poll tick. "Watch personas interact" is a conversation read top-down, but not at the cost of stealing the scroll position.
- **Sender display names *and* a human/agent visual distinction** — the timeline maps `sender_id → name (role)` using the agent list (pairing with §A) rather than showing raw ids, **and** visually distinguishes the operator's own messages from agents' (the timeline today renders every row identically; the chat panel already bolds "You:" vs the persona — the timeline should carry the same affordance).
- **Chat transcript timestamps** — each turn renders its time, matching the timeline. Live turns stamp at send; seeded history (§B) renders each message's wire `timestamp`. This is what makes resumed history read as *memory* rather than as a reprinted buffer.
- **Persona / channel list refresh** — both lists load once on mount and never refresh ([`Chat.svelte`](../../web/src/panels/Chat.svelte) `$effect`, [`ChannelTimeline.svelte`](../../web/src/panels/ChannelTimeline.svelte)). A tester waiting for a persona to boot, or for its `status` to flip to `healthy`, must reload the whole tab. Add a lightweight refresh control (or fold agent-status refresh into the timeline's existing poll cadence) so the list reflects a changing backend without a full reload.
- **Build version in the topbar** — `/api/v1/ui/config` already returns `build.version`; the shell currently ignores it. Surface it.
- **Abortable chat turn** — a cancel control on the in-flight turn (up to the 30 s `chatDefaultTimeout`), wired to `AbortController` on the existing `fetch`. Not streaming (OQ5 stays deferred); it cancels a synchronous request so a 30 s blocking wait is escapable.

### §E. Tester identity override (closes Context #5 — the one decision that bends RFC 0048 §F)

**No API change** — the chat and publish endpoints already accept `user_id` / `sender_id`. The change is *which value the client supplies*, and it is the single most important fix for Goal 4, because persistence is keyed on `(user_id, agent_id)` and the console today freezes `user_id` to the `/ui/context` principal `local` ([`bootstrap.js`](../../web/src/lib/bootstrap.js)).

**Proposal.** The Chat panel (and the timeline publish box) gains an explicit, clearly-labelled local **"Acting as"** override that sets the `user_id`/`sender_id` the client sends. It **defaults to the `/ui/context` principal** and is presented as a *testing control*, not as identity ("Acting as (local testing) — defaults to `local`"). This lets a tester run the headline demonstration end-to-end in the browser: greet the persona as user A, switch to user B, watch the persona not recognise them, switch back, watch it pick up where A left off — *that* is persistence made visible.

**Careful reconciliation with [RFC 0048 §F rule 1](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)** ("Identity is server-provided, never assumed; the console must derive `user_id` from the `/ui/context` principal rather than prompting for or hard-coding it"). The override is a *deliberate, scoped exception*, and it honours the two intents the rule actually protects:

1. **A single server-provided identity *source* still exists.** The override **defaults to** the `/ui/context` principal and is *layered on top of it*, not a replacement source. The console reads identity from exactly one place; the override is an operator-visible offset from that value, not a second source of truth. `/ui/context` is unchanged.
2. **No single-user assumption is baked in.** Rule 1's deeper purpose (with rule 2) is to keep the client from assuming one user so that wiring a real principal later is additive. The override does the *opposite* of a single-user assumption — it surfaces the very multi-user axis the persistence story lives on, which is the forward-compatible shape, not a regression from it.

**Guardrails (normative for PR D):**

- The control is **visibly a local testing affordance**, never styled or labelled as authenticated identity. The topbar continues to show the real `/ui/context` principal verbatim; the override annotates the turn ("sent as `<override>`") so the two are never conflated.
- The override is **inert and hidden once `/ui/context` reports `authenticated: true`** (i.e. once RFC 0039 lands). An authenticated principal can never be silently overridden from the browser; at that point the field disappears (or, if a future design wants user-impersonation for admins, it becomes a separate permission-gated feature — out of scope here). This is what keeps §E a *local-mode carve-out* rather than a hole in the future auth model.
- **No endpoint changes**, no new privileged surface; the override only selects which `user_id` the existing read/write calls carry — exactly what a CLI caller already does by passing `--user`.

This is the one genuine product decision in the amendment (see [Open Questions](#open-questions) #5) and must be signed off before PR D, precisely because it documents a carve-out to a forward-compat rule.

### §F. First-contact onboarding & cross-panel continuity (closes Context #6, #7)

**No API change** (the cross-panel link depends on §B surfacing the resolved DM channel id, which §B already builds). Two client-only affordances, both serving the community-growth Goal directly:

- **Empty states become onboarding, not dead ends.** "No personas are registered yet." / "No channels exist yet." / "No messages yet." each gain a next step — a link to the web-console quick-start ([`docs/guides/web-console.md`](../guides/web-console.md)) and a one-line pointer to how a persona/channel is registered. Justification: a first-contact surface whose empty state is a full stop fails at the one job (lowering first-contact friction, RFC 0048 Motivation) it was built for. This is the cheapest high-leverage fix in the amendment.
- **Cross-panel continuity: chat persists as a watchable channel.** Once a conversation exists, the Chat panel offers a "view this conversation in the timeline" affordance that deep-links the Channel Timeline to the resolved DM channel (the channel `LookupDM` resolves in §B). Justification: the two hero panels' *combined* story — your chat is a real, persisted, watchable channel — is currently impossible to see without the CLI; this turns §B's persistence claim from an assertion into a click. **Dependency:** needs §B's resolved DM channel id surfaced to the client (a small addition to the §B client work — the id is known once history resolves), so PR F follows PR B.

## What this amendment does NOT change

- **It does not add the memory inspector.** §B is *conversation continuity* over the channel-history infra that already backs chat — not the deferred four-tier memory read ([RFC 0048 §G](0048-operator-tester-web-console.md#g-api-gaps--required-backend-work), Slice 2). No Go↔Python boundary crossing; `LookupDM` is a read against the same channel store `GetOrCreateDM` already uses.
- **It does not add auth, multi-tenancy, or write surface.** §B is read-only; §C reuses existing endpoints; the new `role` field is read-only; §F is client-only. **§E adds no auth and no endpoint** — it is a local-mode *testing convenience* that defaults to, never replaces the source of, and is inert once superseded by, the `/ui/context` principal (RFC 0039). It changes which `user_id` the existing calls carry, exactly as a CLI `--user` flag does. The [RFC 0048 §Security](0048-operator-tester-web-console.md#security-considerations) posture (localhost-only, `--enable-ui` off by default, no new privileged endpoints) is preserved — the one new endpoint (§B) is read-only and joins the existing middleware stack.
- **It does not add channel push or chat token streaming** ([OQ4/OQ5](0048-operator-tester-web-console.md#open-questions)) — both remain deferred. The §D abort control is not streaming; it cancels a synchronous request.
- **It does not change `config/ui.yaml`, the toggle model, or `/ui/context`** — the panel set and identity *contract* are untouched. §E layers a client-side offset on top of the principal `/ui/context` provides; it does not alter the endpoint or its payload.

## Implementation

Suggested PR decomposition (each independently shippable; §D items ride with their natural panel):

1. **PR A — Persona legibility (§A + §D sender-rendering).** Add `role` to `agentResponse` + `agentToResponse`; extend the handler tests for the new field. Client: show `name — role` + capability list in a persona header above the transcript and in the picker; map `sender_id → name (role)` in the timeline and add the human/agent visual distinction. Pure-additive API.
2. **PR B — Chat-history continuity (§B).** New `ChannelStore.LookupDM` (read-only DM resolve preserving `GetOrCreateDM`'s membership gate, returns a not-found sentinel cleanly); new `GET /api/v1/agents/{id}/chat/history` handler reusing the channel-history query-param parsing and `historyResponse` shape; handler + store tests (empty-history → `200 []`, populated → newest-first, bad `limit`/`before` → `400`, access-control parity with `GetOrCreateDM`). Client: seed transcript from history on persona-select; migrate to a flat message list **preserving the per-turn scope annotation** (caveat 2); render per-message timestamps (§D); retain the resolved DM channel id for §F.
3. **PR C — Session selector + composer ergonomics (§C + §D).** Client-only: session dropdown over `/api/v1/sessions` with create; Enter-to-send in **both** composers; abortable turn; timeline order reversal + pinned-scroll autoscroll; list refresh; version in topbar. No backend change.
4. **PR D — Tester identity override (§E).** Client-only: "Acting as (local testing)" override defaulting to the `/ui/context` principal, annotating each turn, **inert/hidden when `authenticated: true`**. No backend change. (Decision #5 locked 2026-06-03 — the §E guardrails are normative acceptance criteria.)
5. **PR F — First-contact onboarding + cross-panel continuity (§F).** Client-only: onboarding empty states linking the quick-start; "view this conversation in the timeline" deep-link using PR B's resolved DM channel id. Follows PR B.

PRs A and B touch the backend and want handler/store tests per the [RFC 0048 test strategy](0048-operator-tester-web-console.md#test-strategy) (unit on DTO/handler shape, integration on the new route under `--enable-ui`; the `LookupDM` access-control parity is a unit test against the store). PRs C, D, F are exercised by the Svelte component tests already established for the panels.

## Decisions (locked 2026-06-03)

All six open questions are **resolved** — the amendment is Accepted and ready to implement. Each resolution optimises for the slice's stated goal: a v0.3.6 console that is genuinely usable and convenient, where the persistence/personality differentiator can be *shown*, not just asserted.

1. **§B endpoint shape** — **agent-scoped `/api/v1/agents/{id}/chat/history`.** Solves reload-before-first-message and keeps DM-id derivation server-side; the rejected `channel_id`-on-`chatResponse` alternative does neither.
2. **§B fresh-start status** — **`200` with empty `messages`.** No-history is the expected fresh-start case, not an error; a `404` would force the client to branch error-handling on a normal state.
3. **§A `model` exposure** — **deferred.** Infra detail, not a personality cue; it belongs with the Slice 4 cost panel if anywhere. `role` (+ already-served `capabilities`/`address`) carries persona legibility for Slice 1.
4. **§D timeline order** — **conversational (oldest-top, newest-bottom) with the pinned-scroll guard.** The RFC frames the panel as "watch personas interact" (a top-down conversation); the guard ensures autoscroll never steals a reading operator's scroll position.
5. **§E tester identity override** — **yes: ship it**, local-mode only, defaulting to the `/ui/context` principal, annotated as a testing control, and **inert/hidden once `authenticated: true`**. This is the one resolution that bends [RFC 0048 §F rule 1](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility), so it is documented as a scoped carve-out (§E), not a silent default. Rationale: persistence is keyed on `(user_id, agent_id)`; with identity frozen to `local`, Goal 4's headline demo — *greet me by name, then not recognise a different user* — is undemonstrable from the browser. The guardrails (defaults to the principal, never replaces the identity *source*, disappears under real auth) keep it forward-compatible. Keeping identity frozen would honour §F rule 1 literally at the cost of the very thing the slice exists to show — an unacceptable trade for a usability-focused release.
6. **§F cross-panel link** — **yes: deep-link the chat DM into the timeline.** The cheapest way to make the combined persistence story (*your chat is a real, watchable channel*) a click instead of an assertion; it reuses §B's resolved DM channel id.

These resolutions are exercised through the [Implementation](#implementation) PR decomposition (A–F). PR D (§E) carries the carve-out guardrails as normative acceptance criteria.

## Related documentation

- [RFC 0048 — Operator & Tester Web Console](0048-operator-tester-web-console.md) — the amended spec (§D Slice 1, §F forward-compat, §G API gaps, §Security).
- [RFC 0048 Phase 1 PR plan](0048-phase1-pr-plan.md) — the shipped six-PR workstream this hardens.
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) — the channel store / DM model §B builds on.
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md) — the session axis §C surfaces.
- [RFC 0039 — User Accounts & Authentication](0039-user-accounts-authentication.md) — the auth layer §E's identity-override carve-out is superseded by.
