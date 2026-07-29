// Console login (RFC 0039 Phase 2 — enabled-mode exposure amendment §A4).
//
// The console logs in with `session_transport: "cookie"`: the session rides
// the `__Host-` HttpOnly cookie the orchestrator sets, so the token never
// enters page-readable space — this module deliberately has nothing to store.
// Subsequent same-origin fetches carry the cookie automatically, and writes
// pass the server's §A2 same-origin assertion via the browser's own
// Sec-Fetch-Site header. Logging OUT of the console is a Phase 3 session-
// management concern (RFC 0039 §Non-Goals); PR 6's manual test drives
// /api/v1/auth/logout directly.
//
// No X-Agent-ID here: the auth endpoints mount on the limiter-bypass root
// mux (their own §B login throttles apply), so the console's rate-limit
// identity header would be dead weight on this one path.

// The 401 listener seam. api.js reports every 401 it turns into an ApiError
// through here (a single chokepoint — all console data calls flow through
// errorFromResponse), and App.svelte registers the listener that flips the
// shell into its login state. A plain callback rather than a store keeps
// api.js free of Svelte imports.
let unauthorizedListener = null;

// onUnauthorized registers the (single) 401 listener; pass null to detach.
export function onUnauthorized(listener) {
  unauthorizedListener = listener;
}

// reportUnauthorized notifies the registered listener, if any. Called by
// api.js; safe with none registered (boot happens before App mounts).
export function reportUnauthorized() {
  unauthorizedListener?.();
}

// login verifies a credential and establishes the cookie session. Resolves
// with the server's login payload (participant_id, role, expires_at — no
// token: that is the cookie transport's whole point) or throws an Error
// whose `status` carries the HTTP status (401 invalid credentials, 429
// throttled) so the form can word its message.
export async function login(username, password) {
  let response;
  try {
    response = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        password,
        session_transport: "cookie",
      }),
    });
  } catch (cause) {
    throw new Error("network error reaching the login endpoint", { cause });
  }
  if (!response.ok) {
    let envelope = null;
    try {
      envelope = await response.json();
    } catch {
      envelope = null;
    }
    const message =
      envelope && typeof envelope.error === "string"
        ? envelope.error
        : `login failed (${response.status})`;
    const err = new Error(message);
    err.status = response.status;
    throw err;
  }
  return response.json();
}
