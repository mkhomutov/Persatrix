package channels

// RFC 0031 Phase 3 PR 4 — request-scoped session override.
//
// The ISSUE-0082 per-request `(agent, channel)` auto-binding isolates
// concurrent conversations that share one orchestrator process. An operator
// who passes `persatrix chat --session run-arc-3` (or `channel send
// --session …`) is deliberately asking for a *specific* session — e.g.
// re-binding a dementia-test arc across runs (RFC 0031 OQ #1 resolution 1a) —
// and that explicit intent must win.
//
// The REST handler stamps the resolved override onto the request context with
// [WithSessionOverride]; the dispatch chokepoint ([GRPCMessageDispatcher.Dispatch])
// reads it with [SessionOverrideFromContext] and prefers it over
// [SessionBinder.Resolve] for that one request. Absent an override the
// auto-binding stands, so the default concurrent-isolation property
// (Phase 2 + ISSUE-0082) is byte-identically preserved — the override is
// additive on the explicit path only.
//
// The override travels by context value rather than a struct field because
// it must survive the [context.WithoutCancel] hop the router makes when it
// detaches fanout from the HTTP request lifetime (see [ChannelRouter.fanout])
// — context values are preserved across that boundary, the request itself is
// not.

import "context"

// sessionOverrideKey is the unexported context key under which an explicit
// per-request session override travels from the REST handler to the dispatch
// chokepoint. An unexported zero-size key type cannot collide with keys set
// by other packages on the same context.
type sessionOverrideKey struct{}

// WithSessionOverride returns ctx carrying an explicit session id that the
// dispatcher prefers over the auto-binding for this one request. An empty id
// is a no-op (ctx is returned unchanged) so callers can thread the resolved
// value unconditionally without re-introducing an empty header the persona
// side would ignore — matching [grpcmeta.InjectSession]'s partial-set
// semantics.
func WithSessionOverride(ctx context.Context, sessionID string) context.Context {
	if sessionID == "" {
		return ctx
	}
	return context.WithValue(ctx, sessionOverrideKey{}, sessionID)
}

// SessionOverrideFromContext returns the explicit session override set by
// [WithSessionOverride], or "" when none is present.
func SessionOverrideFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(sessionOverrideKey{}).(string); ok {
		return v
	}
	return ""
}
