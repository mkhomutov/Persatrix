package server

// ISSUE-0082 Part 2 PR 2 (v0.3.14) — the principal PRODUCER, and the
// origin-set enumeration that bounds it.
//
// PR 1 landed the rail dormant: [channels.WithPrincipal] carries a verified
// principal on the request context, [grpcmeta.InjectPrincipal] emits it as
// the `persatrix-principal` header at the dispatch chokepoint, and the
// persona binds a strict-equality `principal_scope` from it. Nothing fed it.
// This file is the source: the RFC 0039 §F verified `participant_id` — the
// same value the chat handler already stamps onto `ChatRequest.user_id` —
// threaded onto the request context so every dispatch descending from that
// request carries it.
//
// # Why the middleware, not each handler
//
// The v0.3.14 plan's emission-surface lock says "the REST handlers thread the
// resolved identity onto the request ctx", and its top-ranked risk is that
// *one origin is missed*: a missed origin fails OPEN — no error, no red test,
// just a silent collapse to `'local'` where two people's rows co-mingle, the
// exact defect this release closes. Threading per handler makes exhaustiveness
// a property of a hand-maintained list; threading at [Server.authMiddleware],
// where the identity is resolved and already stamped under `authIdentityKey`,
// makes it a property of the *composition* — the middleware wraps the root
// mux, so a route added tomorrow inherits the principal without anyone
// remembering to thread it. Same lock, discharged structurally instead of by
// vigilance. (Deviation from the plan's literal wording, recorded here and in
// the PR body; the enumeration below is retained as the deliverable it is.)
//
// The threading is exactly as narrow as the per-handler version would be. The
// principal is stamped iff a real account credential resolved
// ([authIdentity.Authenticated]), which is false for every request under
// `auth.mode: disabled` and for every unauthenticated caller under `enabled`
// — including the whole persona fleet, which holds no accounts and drives the
// §Non-Goals public REST seams (see auth_policy.go). So agents emit nothing
// and collapse to `'local'`, which is the declared agent-origin contract.
//
// Read that contract at its real scope (ISSUE-0082 residual R-2): a persona's
// reply re-enters here as a fresh unauthenticated publish, so every fanout
// descending from it dispatches under `'local'` even when it is relaying what
// an authenticated person just said. In a multi-agent room the relayed
// content therefore lands in the shared tenant. The fix is NOT to let the
// persona send a principal back — the persona binds `principal_scope` from
// that header and recall is strict equality on it, so trusting an
// agent-supplied claim would hand an unauthenticated caller a cross-tenant
// READ primitive. Closing it needs server-side causal tracking; deferred.
//
// # The enumeration
//
// Structural threading removes the *fail-open* hazard but not the reviewer's
// question "which routes actually put a principal on the wire?". That is what
// [dispatchOriginClassification] answers, and principal_route_table_test.go
// pins: it parses this package's own route registrations and fails on any
// pattern the table does not name. A route added without a verdict is a
// compile-green, test-RED omission — the inversion the plan asked for.

import (
	"context"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// withRequestPrincipal returns ctx carrying ident's verified participant id
// as the request principal, or ctx unchanged when no account credential
// resolved.
//
// The predicate is [authIdentity.Authenticated], NOT [Server.authEnforced] —
// the two differ on exactly the request this axis must not mis-tag. Under
// `enabled` an unauthenticated caller on a public route (the persona fleet's
// publish/convene seams) resolves [anonymousIdentity], whose ParticipantID is
// the literal `"local"`. Keying on the mode would stamp that `"local"` as an
// explicit principal: same resolved value, but a *header on the wire* where
// today there is none, and the plan's `disabled`-mode no-delta is a byte-level
// acceptance criterion, not a resolved-value one. Keying on Authenticated
// emits nothing, so an agent turn is indistinguishable from a pre-v0.3.14
// turn all the way down to the metadata.
func withRequestPrincipal(ctx context.Context, ident authIdentity) context.Context {
	if !ident.Authenticated {
		return ctx
	}
	// Empty is a no-op inside WithPrincipal; an authenticated identity always
	// carries a participant (§A binds each account 1:1 to a UserParticipant),
	// so this is threading the resolved value unconditionally, not a guard.
	return channels.WithPrincipal(ctx, ident.ParticipantID)
}

// principalOrigin is one route's verdict in the dispatch-origin enumeration.
type principalOrigin uint8

const (
	// originPrincipalBearing — the route reaches
	// [channels.GRPCMessageDispatcher.Dispatch], so an authenticated caller's
	// principal rides the wire and partitions the persona memory that turn
	// writes and reads. These three are the whole tenant boundary.
	originPrincipalBearing principalOrigin = iota
	// originNonDispatching — the route never reaches the channel dispatcher,
	// so no principal is emitted from it. Reads, administration, and the
	// workflow surface: `POST /api/v1/workflows/run` schedules through the
	// executor's `ExecuteTask` RPC, which has no persona-memory consumer at
	// all (agents/server_servicers.py lifts `persatrix-principal` in
	// `SendChatMessage` and `ReceiveChannelMessage` only), and
	// `POST /api/v1/personas/{participant_id}/recall` reads the CHANNEL store
	// — membership-scoped verbatim messages, a table with no `principal_id`
	// column — never principal-partitioned persona memory. Both are declared
	// here rather than assumed, per the plan.
	originNonDispatching
)

// dispatchOriginClassification is the origin-set enumeration: every route this
// package registers, with its verdict. Keys are the `net/http` mux patterns
// verbatim, so principal_route_table_test.go can diff them against the parsed
// registrations in both directions — an unclassified route fails, and so does
// a stale entry for a route that was renamed or removed.
//
// Note what this table is and is not. It does NOT gate emission — the
// middleware threads structurally and the dispatcher reads the ctx, so the
// table has no runtime effect. It is the reviewable statement of where the
// boundary lands, kept honest by a test. Reclassifying a row here changes
// nothing at runtime; it is the *rows without a verdict* the test catches.
var dispatchOriginClassification = map[string]principalOrigin{
	// ---- The three dispatch origins ----
	//
	// The console composer and `persatrix channel send`. Public policy (the
	// persona fleet's HTTPChannelPublisher shares it), so this route carries
	// BOTH human and agent traffic and the Authenticated predicate is what
	// separates them: a cookie-bearing console publish emits, a persona's
	// reply publish does not.
	"POST /api/v1/channels/{id}/messages": originPrincipalBearing,
	// The chat REPL / console DM. Already reads the §F claim for
	// `user_id` (chat_handler.go); the principal is that same value, so the
	// tenant dimension and the conversation surface name one identity.
	"POST /api/v1/agents/{id}/chat": originPrincipalBearing,
	// The operator Convene button / `persatrix channel convene`. Public
	// policy for the convene timer callback, same split as publish. This is
	// the origin the plan's surface audit found: it calls
	// `ConveneChannel(r.Context(), …)` directly, descending from no publish,
	// so a per-handler threading pass that walked the publish path would have
	// missed it.
	"POST /api/v1/channels/{id}/convene": originPrincipalBearing,

	// ---- Everything else: no dispatch, no principal on the wire ----
	"GET /healthz":                                               originNonDispatching,
	"GET /ui/":                                                   originNonDispatching,
	"GET /api/v1/ui/config":                                      originNonDispatching,
	"GET /api/v1/ui/context":                                     originNonDispatching,
	"POST /api/v1/auth/login":                                    originNonDispatching,
	"POST /api/v1/auth/logout":                                   originNonDispatching,
	"GET /api/v1/auth/whoami":                                    originNonDispatching,
	"POST /api/v1/workflows/run":                                 originNonDispatching,
	"GET /api/v1/workflows":                                      originNonDispatching,
	"GET /api/v1/workflows/{id}/status":                          originNonDispatching,
	"DELETE /api/v1/workflows/{id}":                              originNonDispatching,
	"POST /api/v1/agents/register":                               originNonDispatching,
	"GET /api/v1/agents":                                         originNonDispatching,
	"GET /api/v1/agents/{id}":                                    originNonDispatching,
	"DELETE /api/v1/agents/{id}":                                 originNonDispatching,
	"POST /api/v1/agents/{id}/unquarantine":                      originNonDispatching,
	"GET /api/v1/agents/{id}/chat/history":                       originNonDispatching,
	"GET /api/v1/agents/{id}/interactions/closed":                originNonDispatching,
	"GET /api/v1/executions/{id}/logs":                           originNonDispatching,
	"GET /api/v1/executions/{id}/logs/stream":                    originNonDispatching,
	"GET /api/v1/cost/summary":                                   originNonDispatching,
	"POST /api/v1/sessions":                                      originNonDispatching,
	"GET /api/v1/sessions":                                       originNonDispatching,
	"GET /api/v1/sessions/{id}":                                  originNonDispatching,
	"POST /api/v1/sessions/{id}/archive":                         originNonDispatching,
	"POST /api/v1/channels":                                      originNonDispatching,
	"GET /api/v1/channels":                                       originNonDispatching,
	"GET /api/v1/channels/{id}":                                  originNonDispatching,
	"DELETE /api/v1/channels/{id}":                               originNonDispatching,
	"GET /api/v1/channels/{id}/messages":                         originNonDispatching,
	"GET /api/v1/channels/{id}/activity":                         originNonDispatching,
	"GET /api/v1/channels/{id}/messages/{msg_id}/thread":         originNonDispatching,
	"POST /api/v1/channels/{id}/members":                         originNonDispatching,
	"PATCH /api/v1/channels/{id}/members/{participant_id}":       originNonDispatching,
	"DELETE /api/v1/channels/{id}/members/{participant_id}":      originNonDispatching,
	"GET /api/v1/channels/{id}/members/{participant_id}/history": originNonDispatching,
	"GET /api/v1/channels/{id}/config":                           originNonDispatching,
	"PATCH /api/v1/channels/{id}/config":                         originNonDispatching,
	"POST /api/v1/personas/{participant_id}/recall":              originNonDispatching,
}
