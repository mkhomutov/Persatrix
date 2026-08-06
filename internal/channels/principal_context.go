package channels

// ISSUE-0082 Part 2 PR 1 (v0.3.14) — request-scoped principal carrier.
//
// The per-request verified principal is the tenant half of the ISSUE-0081
// rail: the persona side has carried a `principal_id` storage dimension with
// strict-equality recall since 2026-05-29, resolving the single-tenant
// `'local'` default because the orchestrator emits nothing. This carrier is
// the Go-side hop between the producer and the wire: a REST handler (PR 2)
// that resolved an authenticated identity (RFC 0039 §F `participant_id`)
// stamps it onto the request context with [WithPrincipal]; the dispatch
// chokepoint ([GRPCMessageDispatcher.Dispatch]) reads it with
// [PrincipalFromContext] and emits it as the `persatrix-principal` gRPC
// header. Absent a value nothing is emitted and the persona keeps resolving
// `'local'` — the normal path under `auth.mode: disabled`, for any
// unauthenticated caller, and on every agent/autonomous-origin turn.
//
// In this PR the carrier is DORMANT: no production code calls
// [WithPrincipal] yet, so behaviour is byte-identical. PR 2 lands the
// producer (the origin-set enumeration in the v0.3.14 plan).
//
// Unlike [WithSessionOverride] / [WithEpochOverride] this is not an
// *override* — there is no auto-binding or boot default to beat; the context
// value is the only source. It still travels by context value for the same
// reason: it must survive the [context.WithoutCancel] hop the router makes
// when it detaches fanout from the HTTP request lifetime (see
// [ChannelRouter.fanout]) — context values are preserved across that
// boundary, the request itself is not. That is what implements the plan's
// propagation lock for the hops that descend from the detached request ctx:
// they share it and so carry the principal to their dispatches.
//
// The lock covers descent, NOT every orchestrator-authored dispatch — a path
// that builds a FRESH context reaches [GRPCMessageDispatcher.Dispatch] with
// no principal on it. Two are known: the synthesis-close timeout
// ([ChannelRouter.onSynthesisTimeout] hands `context.Background()` to the
// bounded close, so the close-notification fan it drives descends from no
// request) and `handleConveneChannel`, which calls `ConveneChannel` on the
// raw request ctx without descending from a publish at all. Principal is the
// only axis exposed by such a reset: session re-resolves through the
// SessionResolver and epoch falls back to the boot value, neither of which
// reads a ctx value, while a principal that is not on the ctx is simply not
// emitted — the turn collapses to `'local'`. PR 2 owns closing both (the
// plan's origin-set enumeration); this PR only carries the mechanism.

import "context"

// principalKey is the unexported context key under which the per-request
// verified principal travels from the REST handler to the dispatch
// chokepoint. An unexported zero-size key type cannot collide with keys set
// by other packages (or with [sessionOverrideKey] / [epochOverrideKey]) on
// the same context.
type principalKey struct{}

// WithPrincipal returns ctx carrying the request's verified principal id for
// the dispatcher to emit on every dispatch descending from this request. An
// empty id is a no-op (ctx is returned unchanged) so callers can thread the
// resolved value unconditionally — an unauthenticated resolution yields
// empty, and threading it must leave the context indistinguishable from one
// that was never touched, matching [grpcmeta.InjectPrincipal]'s partial-set
// semantics.
func WithPrincipal(ctx context.Context, principalID string) context.Context {
	if principalID == "" {
		return ctx
	}
	return context.WithValue(ctx, principalKey{}, principalID)
}

// PrincipalFromContext returns the verified principal set by [WithPrincipal],
// or "" when none is present.
func PrincipalFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(principalKey{}).(string); ok {
		return v
	}
	return ""
}
