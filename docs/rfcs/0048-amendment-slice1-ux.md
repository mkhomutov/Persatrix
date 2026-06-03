# RFC 0048 Amendment — Slice 1 Interaction-UX Hardening

**Type**: amendment to [RFC 0048](0048-operator-tester-web-console.md) §D (Slice 1 — Live Interactions) and §G (API Gaps)
**Status**: 🚧 Proposed
**Date**: 2026-06-03
**Trigger**: Pre-v0.3.6 UX review of the shipped Slice 1 console — the two panels are functionally correct and well-instrumented, but as built they present *"another chat box"*, contradicting [RFC 0048 Goal 4](0048-operator-tester-web-console.md#goals) ("make the differentiator visible — persistence and personality"). The most visible console behaviours read as *stateless* and *faceless*, which is fatal for the community-growth thesis the slice exists to serve.
**Supersedes**: nothing — closes experience gaps Slice 1's render-over-existing-API scope left open. All API changes are **additive and backward-compatible**.

---

## Context

Slice 1 ([RFC 0048 §D](0048-operator-tester-web-console.md#d-slice-1--live-interactions-the-hero)) shipped two panels — Chat and Channel Timeline — over today's REST API ([#501](https://github.com/mkhomutov/Persatrix/pull/501), [#502](https://github.com/mkhomutov/Persatrix/pull/502)). A use-pass over the running console surfaced a cluster of issues that are **not bugs** — the code is race-safe, ARIA-correct, and surfaces the server's error envelope faithfully — but where the *experience* undercuts the slice's reason to exist:

1. **The chat presents as stateless.** The transcript is in-memory only ([`Chat.svelte`](../../web/src/panels/Chat.svelte)), yet the conversation is backed by a **persistent DM channel** server-side (`GetOrCreateDM(user_id, agent_id)`, [`chat_handler.go`](../../internal/server/chat_handler.go)). Reload the tab → blank chat. The persona still remembers, but the UI shows a clean slate, which reads as *"it forgot."* The console's most visible behaviour contradicts the persistence pitch.
2. **The persona is faceless.** The picker shows `name (status)` only. `registry.AgentInfo` carries a `Role` field ([`registry.go`](../../internal/registry/registry.go)), but the JSON DTO `agentResponse` ([`types.go`](../../internal/server/types.go)) drops it — the API discards the single best personality cue it already has.
3. **Isolation can't be driven from the browser.** The session/epoch scope is free-text id entry, even though `GET /api/v1/sessions` already returns **labeled** sessions with a `POST` create path ([`server.go`](../../internal/server/server.go)). The RFC's "quietly demonstrates the v0.3.5 isolation story" requires leaving the browser for the CLI to find a session id.
4. **Conversation ergonomics are missing** — no Enter-to-send, and the channel timeline renders newest-first (reading a conversation backwards), among smaller polish gaps.

This amendment specifies the **API contract changes** (two additive fields/endpoints) and the **client behaviour changes** needed to close 1–4. The client-only changes are listed for completeness but are non-normative — they need no backend agreement.

## The amended contract

### §A. Persona identity on the agent DTO (closes Context #2)

`agentResponse` ([`types.go`](../../internal/server/types.go)) gains one normative field:

```go
type agentResponse struct {
    ID           string   `json:"id"`
    Name         string   `json:"name"`
    Role         string   `json:"role"`          // NEW — from registry.AgentInfo.Role; "" when unset
    Address      string   `json:"address"`
    Capabilities []string `json:"capabilities"`
    Status       string   `json:"status"`
}
```

- `role` is populated from `registry.AgentInfo.Role` in `agentToResponse`. It is **additive** — existing clients ignore the new key; the schema for the endpoint (if any) gains an optional field.
- An empty `role` is valid (not every agent declares one); the client falls back to showing no role rather than a placeholder, exactly as it already falls back `name → id`.
- **`model` is explicitly out of scope here.** It is infra detail, not a personality cue, and exposing it on the public agent list is a separate decision (it belongs with the Slice 4 cost panel, if anywhere). This amendment adds `role` only.

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

**Client behaviour:** on persona-select (and on first mount), the Chat panel fetches this endpoint and seeds the transcript from history, then continues appending live turns. The transcript model shifts from `{prompt, reply}` pairs to a **flat message list** keyed by `sender_id === user_id` (you) vs. otherwise (the persona) — which is both simpler and more correct (it naturally handles the persisted ordering and any non-paired messages). A reload now resumes the conversation, making persistence *visible* — the point of the slice.

**Alternative considered (rejected):** surfacing `channel_id` on `chatResponse` and having the client fetch `/channels/{channel_id}/messages`. Rejected because it does not solve the **reload-before-first-message** case (no `channel_id` known yet) and couples the client to DM-id semantics. The dedicated agent-scoped endpoint resolves both and keeps DM-id derivation server-side.

### §C. Session selector over the existing sessions API (closes Context #3)

**No API change.** `GET /api/v1/sessions` (labeled list) and `POST /api/v1/sessions` (create with `label`) already exist ([`session_handlers.go`](../../internal/server/session_handlers.go)). The Chat panel's session scope control changes from a free-text id input to a **dropdown populated from `/api/v1/sessions`** (showing `label`, value `id`), with an inline "new session" affordance that `POST`s a label and selects the result. The epoch control stays free-text (no labeled-epoch list endpoint exists; epoch ids are operator-namespace values, consistent with [RFC 0048 §F rule 3](0048-operator-tester-web-console.md#f-auth--multi-tenancy-forward-compatibility)). When the session registry is unwired (`503`), the control degrades to the current free-text input rather than disappearing.

### §D. Client-only ergonomics (non-normative — no backend agreement needed)

Listed so the amendment's PR scope is complete; none change the API:

- **Enter-to-send** in the chat composer (Enter sends, Shift+Enter inserts a newline) — the universal chat idiom; today the `<textarea>` swallows Enter and the button is the only send path.
- **Conversational timeline order** — the Channel Timeline renders **oldest-top, newest-bottom, autoscroll-to-bottom** for display (the wire fetch stays `DESC`; the client reverses for render). "Watch personas interact" is a conversation, read top-down, with the publish box and newest message co-located at the bottom.
- **Sender display names** — the timeline maps `sender_id → name (role)` using the agent list rather than showing raw ids, pairing with §A.
- **Build version in the topbar** — `/api/v1/ui/config` already returns `build.version`; the shell currently ignores it. Surface it.
- **Abortable chat turn** — a cancel control on the in-flight turn (up to the 30 s `chatDefaultTimeout`), wired to `AbortController` on the existing `fetch`.

## What this amendment does NOT change

- **It does not add the memory inspector.** §B is *conversation continuity* over the channel-history infra that already backs chat — not the deferred four-tier memory read ([RFC 0048 §G](0048-operator-tester-web-console.md#g-api-gaps--required-backend-work), Slice 2). No Go↔Python boundary crossing; `LookupDM` is a read against the same channel store `GetOrCreateDM` already uses.
- **It does not add auth, multi-tenancy, or write surface.** §B is read-only; §C reuses existing endpoints; the new `role` field is read-only. The [RFC 0048 §Security](0048-operator-tester-web-console.md#security-considerations) posture (localhost-only, `--enable-ui` off by default, no new privileged endpoints) is preserved — the one new endpoint is read-only and joins the existing middleware stack.
- **It does not add channel push or chat token streaming** ([OQ4/OQ5](0048-operator-tester-web-console.md#open-questions)) — both remain deferred. The §D abort control is not streaming; it cancels a synchronous request.
- **It does not change `config/ui.yaml`, the toggle model, or `/ui/context`** — the panel set and identity contract are untouched.

## Implementation

Suggested PR decomposition (each independently shippable; §D items can ride with their natural panel):

1. **PR A — Persona identity (§A).** Add `role` to `agentResponse` + `agentToResponse`; extend the handler tests for the new field. Client: show role in the picker option label and a persona header above the transcript; map sender→name in the timeline (§D). Pure-additive API.
2. **PR B — Chat-history continuity (§B).** New `ChannelStore.LookupDM` (read-only DM resolve, returns not-found sentinel cleanly); new `GET /api/v1/agents/{id}/chat/history` handler reusing the channel-history query-param parsing and `historyResponse` shape; handler + store tests (empty-history → `200 []`, populated → newest-first, bad `limit`/`before` → `400`). Client: seed transcript from history on persona-select; migrate the transcript model to a flat message list.
3. **PR C — Session selector + composer ergonomics (§C + §D).** Client-only: session dropdown over `/api/v1/sessions` with create; Enter-to-send; abortable turn; timeline order reversal + autoscroll; version in topbar. No backend change.

PRs A and B touch the backend and want handler/store tests per the [RFC 0048 test strategy](0048-operator-tester-web-console.md#test-strategy) (unit on DTO/handler shape, integration on the new route under `--enable-ui`). PR C is exercised by the Svelte component tests already established for the panels.

## Open questions

1. **§B endpoint shape** — agent-scoped (`/api/v1/agents/{id}/chat/history`, chosen) vs. exposing `channel_id` on `chatResponse` and reusing `/channels/{id}/messages`. Chosen the former (solves reload-before-first-message; keeps DM-id server-side). Confirm before PR B.
2. **§B fresh-start status** — `200` with empty `messages` (chosen) vs. `404`. Chosen `200` (no-history is not an error). Confirm.
3. **§A `model` exposure** — deferred out of this amendment (infra detail, belongs with Slice 4). Confirm the deferral.
4. **§D timeline order** — conversational (oldest-top, chosen) vs. log-tail (newest-top, as shipped). The RFC frames the panel as "watch personas interact" (conversational); confirm the reversal is wanted over the current log-tail order.

## Related documentation

- [RFC 0048 — Operator & Tester Web Console](0048-operator-tester-web-console.md) — the amended spec (§D Slice 1, §F forward-compat, §G API gaps, §Security).
- [RFC 0048 Phase 1 PR plan](0048-phase1-pr-plan.md) — the shipped six-PR workstream this hardens.
- [RFC 0011 — Channels & Bridges](0011-channels-bridges.md) — the channel store / DM model §B builds on.
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md) — the session axis §C surfaces.
