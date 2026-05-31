package channels

// ISSUE-0085 PR 5 — request-scoped epoch override.
//
// PR 4 resolves the per-process epoch once at boot (PERSATRIX_EPOCH) and emits
// it on `persatrix-epoch` on every dispatch via [WithEpoch]. An operator who
// passes `persatrix chat --epoch ci-run-5` (or `channel send --epoch …`) is
// deliberately asking for a *specific* run/test-isolation epoch for that one
// request — e.g. re-running a scenario under a fresh, isolated world — and that
// explicit intent must win over the boot default.
//
// The REST handler stamps the resolved override onto the request context with
// [WithEpochOverride]; the dispatch chokepoint ([GRPCMessageDispatcher.Dispatch])
// reads it with [EpochOverrideFromContext] and prefers it over the boot epoch
// ([GRPCMessageDispatcher.epoch], set by [WithEpoch]) for that one request.
// Absent an override the boot epoch stands, so the default process-global
// behaviour (PR 4) is byte-identically preserved — the override is additive on
// the explicit path only.
//
// The override travels by context value rather than a struct field for the same
// reason as [WithSessionOverride]: it must survive the [context.WithoutCancel]
// hop the router makes when it detaches fanout from the HTTP request lifetime
// (see [ChannelRouter.fanout]) — context values are preserved across that
// boundary, the request itself is not.

import "context"

// epochOverrideKey is the unexported context key under which an explicit
// per-request epoch override travels from the REST handler to the dispatch
// chokepoint. An unexported zero-size key type cannot collide with keys set by
// other packages (or with [sessionOverrideKey]) on the same context.
type epochOverrideKey struct{}

// WithEpochOverride returns ctx carrying an explicit epoch id that the
// dispatcher prefers over the boot epoch for this one request. An empty id is a
// no-op (ctx is returned unchanged) so callers can thread the resolved value
// unconditionally without re-introducing an empty header the persona side would
// ignore — matching [grpcmeta.InjectEpoch]'s partial-set semantics.
func WithEpochOverride(ctx context.Context, epochID string) context.Context {
	if epochID == "" {
		return ctx
	}
	return context.WithValue(ctx, epochOverrideKey{}, epochID)
}

// EpochOverrideFromContext returns the explicit epoch override set by
// [WithEpochOverride], or "" when none is present.
func EpochOverrideFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(epochOverrideKey{}).(string); ok {
		return v
	}
	return ""
}
