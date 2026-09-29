// Thin typed-ish client for the console's backend (RFC 0048 Phase 1 / Slice 1).
//
// PR 3 needs only the two boot endpoints; PR 4 extends this module with the
// chat/agents calls the chat panel drives; PR 5 adds the channel list/history/
// publish calls the timeline panel drives. All calls are same-origin (the SPA
// is served by the orchestrator under /ui), so paths are root-relative and no
// base URL or CORS handling is required.

import { ApiError, getJSON, postJSON, patchJSON } from "./http.js";

// Re-exported so the panels keep importing the error type from here.
export { ApiError };

// loadBootstrap fetches the two read-only boot endpoints concurrently and
// returns { config, context }. The SPA cannot render its panels without both
// (config decides which panels, context supplies the principal the panels act
// as), so a failure in either rejects — the shell renders a boot-error state
// rather than a half-configured console.
export async function loadBootstrap() {
  const [config, context] = await Promise.all([
    getJSON("/api/v1/ui/config"),
    getJSON("/api/v1/ui/context"),
  ]);
  return { config, context };
}

// listAgents fetches the registered personas (GET /api/v1/agents) the chat
// panel offers in its picker. Each entry is the server's `agentResponse`
// ({id, name, status, …}); the panel reads `name`/`id` for the label, so no
// per-agent GET /api/v1/agents/{id} is needed — the list already carries the
// display name.
export async function listAgents() {
  return getJSON("/api/v1/agents");
}

// sendChat issues the one synchronous chat turn (POST /api/v1/agents/{id}/chat)
// and returns the parsed `chatResponse` ({reply, agent_display_name, …}). It
// owns the wire contract the panel must not get wrong:
//   - `participant_type:"user"` is always sent explicitly. The handler now
//     defaults an omitted value to "user" (chat_handler.go ISSUE-0068), so this
//     is belt-and-suspenders rather than load-bearing — but it keeps the human
//     peer tagged on the wire instead of relying on a server-side default the
//     panel can't see, and stays correct against any caller path lacking it;
//   - `user_id` is the /ui/context-derived principal the caller passes in, never
//     prompted (RFC §F rule 1);
//   - `session_id` / `epoch_id` ride only when supplied, so an unset selector
//     leaves the orchestrator's boot defaults intact (RFC 0031 / ISSUE-0085)
//     rather than pinning the conversation to an empty override.
export async function sendChat(
  agentID,
  { message, userId, sessionId, epochId, signal },
) {
  const body = { message, user_id: userId, participant_type: "user" };
  if (sessionId) {
    body.session_id = sessionId;
  }
  if (epochId) {
    body.epoch_id = epochId;
  }
  // Encode the id rather than interpolating it raw: it comes from the server's
  // own agent list today (a constrained registry key), but encoding keeps the
  // request pinned to the /agents/{id}/chat route for any id, instead of
  // relying on that assumption holding.
  return postJSON(`/api/v1/agents/${encodeURIComponent(agentID)}/chat`, body, {
    signal,
  });
}

// listSessions fetches the labeled operator sessions (GET /api/v1/sessions) the
// chat panel offers as a dropdown, so the v0.3.5 isolation story is drivable
// from the browser without leaving for the CLI to find a session id (RFC 0048
// amendment §C). Returns the `listSessionsResponse` envelope ({sessions}); each
// entry is {id, label?, created_at, archived}. A 503 (session registry unwired)
// surfaces as an ApiError so the panel can degrade to free-text entry.
export async function listSessions() {
  return getJSON("/api/v1/sessions");
}

// createSession mints a labeled session (POST /api/v1/sessions) and returns the
// stored `sessionResponse`. `label` is required server-side; the panel selects
// the returned id after creating.
export async function createSession(label) {
  return postJSON("/api/v1/sessions", { label });
}

// getChatHistory resumes a conversation read-only
// (GET /api/v1/agents/{id}/chat/history?user_id=…), returning the `historyResponse`
// envelope ({messages}) — the SAME shape as getChannelHistory, newest-first, so
// the chat panel reuses that parsing to seed its transcript on reload (RFC 0048
// amendment §B). The server resolves the canonical DM for (user_id, agent_id)
// without creating it; a persona never chatted with returns `200` with an empty
// messages array (not 404), so the caller treats no-history as a normal empty
// conversation rather than an error. `user_id` is required (it is half the DM
// key); `limit`/`before` mirror getChannelHistory and ride only when supplied.
export async function getChatHistory(agentID, { userId, limit, before } = {}) {
  // user_id is half the DM key and has no sane default for a read (unlike the
  // chat POST's shared-"local" fallback). Guard here so a missing principal
  // fails at the call site rather than serialising to the literal string
  // "user_id=undefined" — which the server would resolve as a real user named
  // "undefined", answering 200-empty and silently masking the bug.
  if (!userId) {
    throw new Error("getChatHistory requires a userId");
  }
  const params = new URLSearchParams();
  params.set("user_id", userId);
  if (limit) {
    params.set("limit", String(limit));
  }
  if (before) {
    params.set("before", before);
  }
  return getJSON(
    `/api/v1/agents/${encodeURIComponent(agentID)}/chat/history?${params.toString()}`,
  );
}

// listChannels fetches EVERY channel the conversation list offers
// (GET /api/v1/channels) and returns them in one `{channels}` envelope. The
// server pages by 50 in channel-id order and returns a `next_cursor` while more
// rows exist (ISSUE-0015, channel_types.go); DM ids (`dm:…`) sort ahead of
// group ids (`group:…`), so reading only the first page hid every group channel
// once a deployment held 50 DMs. The walk follows the cursor to the last page,
// stops if a cursor fails to advance, and is bounded by MAX_CHANNEL_PAGES
// (2 000 channels) so a misbehaving server cannot spin it forever.
const MAX_CHANNEL_PAGES = 40;

export async function listChannels() {
  const channels = [];
  let cursor = "";
  for (let page = 0; page < MAX_CHANNEL_PAGES; page++) {
    const path = cursor
      ? `/api/v1/channels?cursor=${encodeURIComponent(cursor)}`
      : "/api/v1/channels";
    const body = await getJSON(path);
    channels.push(...(body?.channels ?? []));
    const next = body?.next_cursor ?? "";
    if (!next || next === cursor) {
      break;
    }
    cursor = next;
  }
  return { channels };
}

// getChannelHistory fetches a channel's message history
// (GET /api/v1/channels/{id}/messages), returning the `historyResponse`
// envelope ({messages}) — already newest-first on the wire (sqlite_messages.go
// `ORDER BY timestamp DESC`), so the panel renders it without re-sorting. The
// optional `limit` (positive int) and `before` (RFC-3339 cursor) ride only when
// supplied — both error loudly server-side on a malformed value
// (channel_query_params.go), so the head-poll passes just `limit` and a
// paginating back-fill adds `before`.
export async function getChannelHistory(channelID, { limit, before } = {}) {
  const params = new URLSearchParams();
  if (limit) {
    params.set("limit", String(limit));
  }
  if (before) {
    params.set("before", before);
  }
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  // Encode the id (DM ids carry colons, e.g. `dm:a:b`) so the request stays
  // pinned to the {id}/messages route for any channel id.
  return getJSON(
    `/api/v1/channels/${encodeURIComponent(channelID)}/messages${suffix}`,
  );
}

// getChannelActivity reads a channel's in-flight "thinking" set
// (GET /api/v1/channels/{id}/activity), returning the `channelActivityResponse`
// envelope ({thinking}) — the participant ids the orchestrator has dispatched a
// turn to and is awaiting a reply from (RFC 0048 console presence Tier 1). This
// is the authoritative signal behind the "… is thinking" indicator: unlike the
// optimistic client overlay it is accurate for every trigger and survives a
// reload. `thinking` is always an array (the server marshals an idle channel as
// []). The id is encoded like every other channel route (canonical ids carry a
// type-prefix colon, e.g. "group:planning"). The console only calls this for
// GROUP channels — a DM's presence is fully owned by its synchronous send
// lifecycle (see lib/presence.svelte.js).
export async function getChannelActivity(channelID) {
  return getJSON(
    `/api/v1/channels/${encodeURIComponent(channelID)}/activity`,
  );
}

// getClosedInteractions reads an agent's closed-interaction summaries
// (GET /api/v1/agents/{id}/interactions/closed), returning the
// `closedInteractionsResponse` envelope ({interactions}) newest-first
// (interactions_handler.go). This is the read side of the v0.3.8
// interaction-summary surface (RFC 0020 §C/§D): when an interaction closes (by
// vote / cost / idle) the persona persists a synthesised summary, and the
// conversation view surfaces it. The endpoint is per-agent — each participating
// persona persists its own summary row — so the surface queries the channel's
// agents and merges (see interactions.js `pickLatestClosed`). The optional
// `scope` (the channel id / RFC 0020 scope), `interactionId`, `limit` and
// `minTurns` ride only when supplied; the server forwards the
// "[interaction summary unavailable]" sentinel verbatim so a failed summary is
// shown honestly rather than blanked.
export async function getClosedInteractions(
  agentID,
  { scope, interactionId, limit, minTurns } = {},
) {
  const params = new URLSearchParams();
  if (scope) {
    params.set("scope", scope);
  }
  if (interactionId) {
    params.set("interaction_id", interactionId);
  }
  if (limit) {
    params.set("limit", String(limit));
  }
  if (minTurns) {
    params.set("min_turns", String(minTurns));
  }
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  // Encode the id (DM ids carry colons) so the request stays pinned to the
  // {id}/interactions/closed route for any agent id.
  return getJSON(
    `/api/v1/agents/${encodeURIComponent(agentID)}/interactions/closed${suffix}`,
  );
}

// createChannel creates a group channel (POST /api/v1/channels) and returns the
// stored channel. The server derives the canonical id `group:<name>` from
// `name` (channel_handlers.go handleCreateChannel), so the caller passes the
// bare name — prepending `group:` here would yield `group:group:<name>` (RFC
// 0048 channel-creation amendment §B). `members` is the non-empty
// `[{ id, respond }]` array the endpoint requires (each id comes from the
// server's own agent list, never free-typed — amendment §C); `description`
// rides only when supplied. A `409 CONFLICT` (the `group:<name>` already exists)
// surfaces as an ApiError whose message carries the server's wording, so the
// form can show a duplicate-name retry as a clear conflict.
export async function createChannel({ name, description, members }) {
  const body = { name, members };
  if (description) {
    body.description = description;
  }
  return postJSON("/api/v1/channels", body);
}

// publishMessage posts a human message into a channel
// (POST /api/v1/channels/{id}/messages) and returns the stored
// `channelMessageResponse`. `sender_id` is REQUIRED by the handler
// (channel_handlers.go) and is the /ui/context-derived principal the caller
// passes in — never free-text (RFC §F rule 1). `mentions` is the optional RFC
// 0011 array of member ids the composer lifted from `@id` tokens; it rides only
// when non-empty so a plain publish keeps the pre-feature wire shape (the server
// field is `omitempty`), and the agent mention fan-out surfaces on the next poll.
export async function publishMessage(
  channelID,
  { senderId, content, mentions = [] },
) {
  const body = { sender_id: senderId, content };
  if (mentions.length > 0) {
    body.mentions = mentions;
  }
  return postJSON(
    `/api/v1/channels/${encodeURIComponent(channelID)}/messages`,
    body,
  );
}

// getChannelConfig reads a channel's effective governance config
// (GET /api/v1/channels/{id}/config), returning the `channelConfigResponse`
// ({revision, <eight knobs>}) where each knob is a {value, source} pair —
// `source` ("channel" | "default") is the provenance the panel renders
// (overridden-here vs inherited default). One knob, `interaction_budget_tokens`,
// reads back `value: null` when inherited (not router-held — RFC 0050 Phase 1
// Open item 4); the panel must treat that as "inherited, unset," not coerce to
// 0. The surface is gated behind `config_edit_enabled`, so a 403 (off) / 503
// (store/router unwired) surfaces as an ApiError carrying the server's wording.
export async function getChannelConfig(channelID) {
  // Encode the id (canonical ids carry a type-prefix colon, e.g.
  // "group:planning") so the request stays pinned to the {id}/config route.
  return getJSON(`/api/v1/channels/${encodeURIComponent(channelID)}/config`);
}

// patchChannelConfig applies a sparse governance-config edit
// (PATCH /api/v1/channels/{id}/config) under an optimistic-concurrency guard,
// returning the new `channelConfigResponse` (with the bumped revision) for reuse
// as the next If-Match without a re-read. `patch` is the sparse `{knob: value}`
// body: explicit `null` means unset→inherit, an absent key means leave-unchanged.
// JSON.stringify keeps an explicit null but DROPS `undefined`, so a revert must
// pass `null`, never `undefined`. `revision` (last read) rides the REQUIRED
// `If-Match` header as a bare integer (an absent header is a 428, not a
// lost-update write). The full status set — 403 (toggle off), 409 (conflict),
// 428 (If-Match missing), 400 (bad knob / unparseable If-Match), 404 (no
// channel), 503 (store/router unwired) — survives onto ApiError with status
// intact, so the panel can branch on 409 to reload-not-overwrite (RFC 0050 P2).
export async function patchChannelConfig(channelID, patch, revision) {
  // Guard the required `revision` at the call site so a missing/garbage value
  // fails here rather than serialising to "undefined"/"NaN" in If-Match (a
  // guaranteed 400). Number.isInteger admits 0 (first revision) but rejects
  // undefined/null/NaN/fractional/string.
  if (!Number.isInteger(revision)) {
    throw new Error(
      "patchChannelConfig requires an integer revision (the value last read)",
    );
  }
  return patchJSON(
    `/api/v1/channels/${encodeURIComponent(channelID)}/config`,
    patch,
    { "If-Match": String(revision) },
  );
}

// conveneChannel opens an autonomous channel (RFC 0052 §B) — the operator action
// behind the Channel-settings "Convene" button. POST /api/v1/channels/{id}/convene
// dispatches the convene forced turn to the channel's configured
// `autonomous.convener`, which authors the opening turn; the discussion then
// sustains itself. Returns the 202 `{channel_id, convener, status}` ack. The
// endpoint is gated behind the same `config_edit_enabled` toggle as the config
// surface, so 403 (off) / 404 (no such channel) / 409 (not autonomous.enabled,
// already convening, no open-floor responder, or no topic/agenda/goal) / 400
// (drifted convener) / 503 (store/router unwired) survive onto ApiError with the
// server's wording.
export async function conveneChannel(channelID) {
  // No body — the topic/agenda/goal come from the channel's persisted
  // `autonomous` config, not the request. Encode the id (canonical ids carry a
  // type-prefix colon) so the request stays pinned to the {id}/convene route.
  return postJSON(`/api/v1/channels/${encodeURIComponent(channelID)}/convene`, {});
}
