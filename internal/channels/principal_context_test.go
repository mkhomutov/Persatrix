package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
)

// ISSUE-0082 Part 2 PR 1 — request-scoped principal carrier.
//
// These tests pin the context carrier in isolation; the dispatcher's
// emission is pinned in grpc_dispatcher_principal_test.go. The carrier is
// dormant in this PR (no production caller), so the pins here are the whole
// behavioural surface until PR 2 lands the producer.

func TestPrincipal_RoundTrips(t *testing.T) {
	ctx := WithPrincipal(context.Background(), "user-alice")
	assert.Equal(t, "user-alice", PrincipalFromContext(ctx),
		"WithPrincipal must round-trip through PrincipalFromContext")
}

func TestPrincipal_AbsentIsEmpty(t *testing.T) {
	assert.Empty(t, PrincipalFromContext(context.Background()),
		"a context with no principal must read back as the empty string")
}

func TestPrincipal_EmptyIsNoOp(t *testing.T) {
	// An empty id must not attach a value — an unauthenticated resolution
	// yields empty, and threading it must leave the context
	// indistinguishable from one that was never touched (so the dispatcher
	// emits nothing and the persona resolves 'local', not an empty header).
	ctx := WithPrincipal(context.Background(), "")
	assert.Empty(t, PrincipalFromContext(ctx),
		"an empty principal must be a no-op (nothing emitted)")
}

func TestPrincipal_LastWriteWins(t *testing.T) {
	// Defensive: a re-thread takes the most recent value, matching
	// context.WithValue shadowing semantics.
	ctx := WithPrincipal(context.Background(), "first")
	ctx = WithPrincipal(ctx, "second")
	assert.Equal(t, "second", PrincipalFromContext(ctx))
}

func TestPrincipal_SurvivesWithoutCancel(t *testing.T) {
	// The load-bearing property for the plan's propagation lock: the router
	// detaches fanout from the HTTP request lifetime via
	// context.WithoutCancel, and every orchestrator-authored hop descending
	// from the publish dispatches on that detached ctx. The principal must
	// survive the hop or no descendant dispatch would ever carry it.
	ctx := WithPrincipal(context.Background(), "user-alice")
	detached := context.WithoutCancel(ctx)
	assert.Equal(t, "user-alice", PrincipalFromContext(detached),
		"the principal must survive the WithoutCancel fanout hop")
}
