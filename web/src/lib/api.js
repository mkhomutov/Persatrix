// Thin typed-ish client for the console's backend (RFC 0048 Phase 1 / Slice 1).
//
// PR 3 needs only the two boot endpoints; PR 4 extends this module with the
// chat/agents calls the chat panel drives. All calls are same-origin (the SPA
// is served by the orchestrator under /ui), so paths are root-relative and no
// base URL or CORS handling is required.

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
    response = await fetch(path);
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
async function postJSON(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
export async function sendChat(agentID, { message, userId, sessionId, epochId }) {
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
  return postJSON(`/api/v1/agents/${encodeURIComponent(agentID)}/chat`, body);
}
