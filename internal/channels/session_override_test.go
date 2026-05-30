package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
)

// RFC 0031 Phase 3 PR 4 — request-scoped session override.
//
// The override is the explicit operator signal (`--session`) the REST handler
// threads onto the request context so the dispatch chokepoint can prefer it
// over the ISSUE-0082 auto-binding for exactly one request. These tests pin
// the context carrier in isolation; the dispatcher's preference logic is
// pinned in grpc_dispatcher_session_test.go.

func TestSessionOverride_RoundTrips(t *testing.T) {
	ctx := WithSessionOverride(context.Background(), "run-arc-3")
	assert.Equal(t, "run-arc-3", SessionOverrideFromContext(ctx),
		"WithSessionOverride must round-trip through SessionOverrideFromContext")
}

func TestSessionOverride_AbsentIsEmpty(t *testing.T) {
	assert.Empty(t, SessionOverrideFromContext(context.Background()),
		"a context with no override must read back as the empty string")
}

func TestSessionOverride_EmptyIsNoOp(t *testing.T) {
	// An empty id must not attach a value — the precedence resolver returns
	// "no session" as the empty string, and threading it must leave the
	// context indistinguishable from one that was never touched (so the
	// dispatcher falls through to the auto-binding, not to an empty override).
	ctx := WithSessionOverride(context.Background(), "")
	assert.Empty(t, SessionOverrideFromContext(ctx),
		"an empty override must be a no-op (auto-binding stands)")
}

func TestSessionOverride_LastWriteWins(t *testing.T) {
	// Defensive: a re-thread (e.g. a wrapper that overrides again) takes the
	// most recent value, matching context.WithValue shadowing semantics.
	ctx := WithSessionOverride(context.Background(), "first")
	ctx = WithSessionOverride(ctx, "second")
	assert.Equal(t, "second", SessionOverrideFromContext(ctx))
}
