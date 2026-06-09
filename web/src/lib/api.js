// Thin typed-ish client for the console's backend (RFC 0048 Phase 1 / Slice 1).
//
// PR 3 needs only the two boot endpoints; PR 4 extends this module with the
// chat/agents calls the chat panel drives; PR 5 adds the channel list/history/
// publish calls the timeline panel drives. All calls are same-origin (the SPA
// is served by the orchestrator under /ui), so paths are root-relative and no
// base URL or CORS handling is required.

// Every console request identifies as this agent id via the X-Agent-ID header
// (server-side security.AgentIDHeader). Without it the orchestrator buckets the
// ENTIRE UI — every panel, tab, and operator — into the shared `anonymous`
// rate-limit bucket, alongside health probes and pre-Phase-4 callers. The
// console's own polling (the 3 s message head-poll plus the per-participant
// interaction-summary fan-out) then trips the per-agent 60-calls/60 s limit and
// surfaces a spurious "Live updates paused: … responded 429" banner during
// normal use — the limiter cannot tell the operator console from a misbehaving
// agent because the console claims no identity. A single stable id parks all
// console traffic in one predictable bucket the operator surface owns, distinct
// from real agents. It is self-reported (token validation lands in RFC 0009
// Phase 4), which is acceptable for the localhost operator console. The value
// must satisfy the server's id schema `^[a-z0-9][a-z0-9-]*[a-z0-9]$`.
const CONSOLE_AGENT_ID = "web-console";
const AGENT_ID_HEADER = "X-Agent-ID";

// consoleHeaders merges the console's agent-id header into an optional base
// header set, so every request out of this client is attributed rather than
// anonymous. A `undefined` base (a bodyless request) yields just the agent-id
// header.
function consoleHeaders(base) {
  return { ...base, [AGENT_ID_HEADER]: CONSOLE_AGENT_ID };
}

// ApiError carries the HTTP status of a non-2xx response so callers can
// distinguish "console couldn't reach its own backend" (the boot path) from a
// transport failure, and surface the server's error envelope to the user (the
// chat panel leans on this for the over-length / bad-request paths). When the
// non-2xx body is the server's `{error, code}` envelope, `message` is the
// server's own wording and `code` is its machine code (e.g. "BAD_REQUEST") so
// panels can show the backend's reason verbatim. A transport failure (fetch
// rejecting) is reported as status 0 with the original error threaded through
// the standard Error `cause` (via `options`) so it is not lost.
export class ApiError extends Error {
  constructor(message, status, options) {
    super(message, options);
    this.name = "ApiError";
    this.status = status;
    this.code = options?.code;
  }
}

// errorFromResponse builds an ApiError from a non-2xx response, preferring the
// server's `{error, code}` envelope (helpers.go `writeError`) so the user sees
// the backend's own wording. A non-JSON or envelope-less error body (a proxy
// page, a bare status) degrades to a generic message keyed on the status — the
// caller still gets a typed failure, just without the server's text.
async function errorFromResponse(path, response) {
  let envelope;
  try {
    envelope = await response.json();
  } catch {
    envelope = null;
  }
  const message =
    envelope && typeof envelope.error === "string"
      ? envelope.error
      : `${path} responded ${response.status}`;
  return new ApiError(message, response.status, { code: envelope?.code });
}

async function getJSON(path) {
  let response;
  try {
    response = await fetch(path, { headers: consoleHeaders() });
  } catch (cause) {
    throw new ApiError(`network error fetching ${path}`, 0, { cause });
  }
  if (!response.ok) {
    throw await errorFromResponse(path, response);
  }
  try {
    return await response.json();
  } catch (cause) {
    // A 2xx with a non-JSON body (a proxy/error page served as 200) reaches
    // here. Wrap the raw SyntaxError so every failure out of this client is an
    // ApiError — the status is the real HTTP status (the response was OK, the
    // body was not), with the parse error threaded through `cause`.
    throw new ApiError(
      `${path} returned a malformed JSON body`,
      response.status,
      { cause },
    );
  }
}

// postJSON sends `body` as JSON to `path`. The orchestrator's handlers reject a
// missing/incorrect Content-Type up front (helpers.go `requireJSON`), so the
// header is mandatory, not cosmetic. On a non-2xx the server's `{error, code}`
// envelope is surfaced as the ApiError (so the panel shows the backend's
// wording); a transport failure is status 0 with the cause preserved, matching
// getJSON's boot-path contract.
async function postJSON(path, body, { signal } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: consoleHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      // An optional AbortSignal lets a caller cancel an in-flight request (the
      // chat panel wires this to a Cancel control so a 30 s synchronous turn is
      // escapable — RFC 0048 amendment §D). When the caller aborts, fetch
      // rejects with an AbortError, surfaced below as a status-0 ApiError.
      signal,
    });
  } catch (cause) {
    throw new ApiError(`network error posting ${path}`, 0, { cause });
  }
  if (!response.ok) {
    throw await errorFromResponse(path, response);
  }
  try {
    return await response.json();
  } catch (cause) {
    throw new ApiError(
      `${path} returned a malformed JSON body`,
      response.status,
      { cause },
    );
  }
}

// sendNoBody issues a write whose success answer is `204 No Content` (the
// member add/remove handlers return no body). It mirrors postJSON's error
// contract — the server's `{error, code}` envelope on a non-2xx, a status-0
// ApiError on a transport failure — but does NOT parse a success body. A JSON
// `body` rides only when supplied (DELETE sends none); the Content-Type header
// is set only then so a bodyless request doesn't claim a JSON payload.
async function sendNoBody(method, path, body) {
  const hasBody = body !== undefined;
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: consoleHeaders(
        hasBody ? { "Content-Type": "application/json" } : undefined,
      ),
      body: hasBody ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    throw new ApiError(`network error on ${method} ${path}`, 0, { cause });
  }
  if (!response.ok) {
    throw await errorFromResponse(path, response);
  }
}

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

// listChannels fetches the channels the timeline panel offers in its picker
// (GET /api/v1/channels). It returns the server's `listChannelsResponse`
// envelope ({channels, next_cursor}) verbatim rather than unwrapping to a bare
// array: the panel reads `.channels`, and echoing the envelope keeps the
// `next_cursor` cursor available for the keyset pagination a later slice may add
// (channel_types.go), instead of discarding it at the client boundary.
export async function listChannels() {
  return getJSON("/api/v1/channels");
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

// addChannelMember adds a participant to an existing channel
// (POST /api/v1/channels/{id}/members → 204). `respond` is one of the
// `channels.RespondPolicy` tokens (when_mentioned/always/never plus the v0.3.8
// participant/chair/addressed/observer vocabulary); the server normalizes the
// open-floor dispositions to the legacy triple and stamps the salience signal,
// so a re-list reads back `respond:"always"` + `salience_gated:true` for a
// chair/participant (channel_handlers.go handleAddChannelMember). The add is
// idempotent — the store inserts `ON CONFLICT DO NOTHING`, so re-adding an
// existing member is a no-op 204 (keeping the original joined_at/policy), NOT a
// conflict. Resolves with no value on success; the error paths are a 404 (no
// such channel) and a 400 (missing id / unknown disposition), surfaced as an
// ApiError carrying the server's wording.
export async function addChannelMember(channelID, { id, respond }) {
  await sendNoBody(
    "POST",
    `/api/v1/channels/${encodeURIComponent(channelID)}/members`,
    { id, respond },
  );
}

// removeChannelMember removes a participant from a channel
// (DELETE /api/v1/channels/{id}/members/{participant_id} → 204). Resolves with
// no value on success; a 404 (channel or member absent) surfaces as an ApiError.
export async function removeChannelMember(channelID, participantID) {
  await sendNoBody(
    "DELETE",
    `/api/v1/channels/${encodeURIComponent(channelID)}/members/${encodeURIComponent(participantID)}`,
  );
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
