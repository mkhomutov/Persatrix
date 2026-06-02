// Thin typed-ish client for the console's backend (RFC 0048 Phase 1 / Slice 1).
//
// PR 3 needs only the two boot endpoints; PRs 4–5 extend this module with the
// chat/agents and channel calls their panels drive. All calls are same-origin
// (the SPA is served by the orchestrator under /ui), so paths are root-relative
// and no base URL or CORS handling is required.

// ApiError carries the HTTP status of a non-2xx response so callers can
// distinguish "console couldn't reach its own backend" (the boot path) from a
// transport failure, and surface the server's error envelope to the user
// (PRs 4–5 lean on this for the chat/channel error paths). A transport failure
// (fetch rejecting) is reported as status 0 with the original error threaded
// through the standard Error `cause` (via `options`) so it is not lost.
export class ApiError extends Error {
  constructor(message, status, options) {
    super(message, options);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJSON(path) {
  let response;
  try {
    response = await fetch(path);
  } catch (cause) {
    throw new ApiError(`network error fetching ${path}`, 0, { cause });
  }
  if (!response.ok) {
    throw new ApiError(`${path} responded ${response.status}`, response.status);
  }
  return response.json();
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
