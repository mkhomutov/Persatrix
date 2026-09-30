// The console's HTTP transport (RFC 0048): same-origin fetch with the console
// agent-id header, the server's `{error, code}` envelope mapped onto ApiError,
// and the 401 seam that flips the shell into its login form. Split out of
// api.js, which keeps one function per endpoint on top of these helpers.
import { reportUnauthorized } from "./auth.js";

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
// must satisfy the server's id schema `^[a-z0-9][a-z0-9-]*[a-z0-9]$` and must
// match the server's `security.ConsoleAgentID`: the orchestrator wires that id
// as a circuit-breaker exemption so a console that trips its own rate limit
// gets a self-clearing 429 rather than a sticky quarantine (a quarantine has no
// automatic recovery and no console UI to clear it).
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
  if (response.status === 401) {
    // RFC 0039 enabled mode: every data call flows through here, so this
    // is the single seam that flips the shell into its login state
    // (amendment §A4 — "a minimal login form on 401").
    reportUnauthorized();
  }
  return new ApiError(message, response.status, { code: envelope?.code });
}

export async function getJSON(path) {
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
export async function postJSON(path, body, { signal } = {}) {
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

// patchJSON sends `body` as a JSON PATCH to `path` with an optional set of extra
// headers (RFC 0050 Phase 2 wires the `If-Match` optimistic-concurrency header
// through here). It mirrors postJSON's contract — the server's `{error, code}`
// envelope on a non-2xx (status survives onto ApiError; the config panel
// branches on 409), a status-0 ApiError on a transport failure — and parses the
// 2xx body (the apply path returns the new effective config + bumped revision).
export async function patchJSON(path, body, extraHeaders) {
  let response;
  try {
    response = await fetch(path, {
      method: "PATCH",
      headers: consoleHeaders({
        "Content-Type": "application/json",
        ...extraHeaders,
      }),
      body: JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(`network error patching ${path}`, 0, { cause });
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
export async function sendNoBody(method, path, body) {
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
