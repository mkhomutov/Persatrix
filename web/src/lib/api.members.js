// Channel member-roster writes (RFC 0011 §C add/remove + RFC 0050 member-config
// edit). Split out of api.js so that module stays under the repo's file-size cap;
// the member *tests* already live in their own api.members.test.js. All three
// answer 204 No Content and reuse api.js's sendNoBody (shared error contract: the
// server's {error, code} envelope on a non-2xx, a status-0 ApiError on transport
// failure), so they parse no success body.
import { sendNoBody } from "./api.js";

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

// updateChannelMember edits an existing member's disposition + salience threshold
// (PATCH /api/v1/channels/{id}/members/{participant_id} → 204, RFC 0050
// member-config edit). It is a full REPLACE of the member's editable config:
// `respond` is REQUIRED by the server (a threshold-only body is a 400) because
// `salience_gated` is re-derived from the *declared* disposition and cannot be
// recovered from persisted state — so the caller always passes the member's
// current/chosen disposition. `threshold` is a number to set the salience bar or
// `null` to unset it (bias-to-silence). Resolves with no value on success; the
// error paths are a 400 (out-of-range threshold, or a threshold on a
// non-open-floor disposition) and a 404 (channel or member absent), each an
// ApiError carrying the server's wording.
export async function updateChannelMember(channelID, participantID, { respond, threshold }) {
  await sendNoBody(
    "PATCH",
    `/api/v1/channels/${encodeURIComponent(channelID)}/members/${encodeURIComponent(participantID)}`,
    { respond, threshold },
  );
}
