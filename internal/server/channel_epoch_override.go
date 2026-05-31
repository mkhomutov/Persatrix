// Per-request epoch override — ISSUE-0085 PR 5 (operator surface).
//
// PR 4 resolves the per-process epoch once at boot from PERSATRIX_EPOCH
// (default "live") and the dispatcher emits it on the `persatrix-epoch` gRPC
// header on every dispatch. This file adds the operator override: a `--epoch`
// flag on the dispatch-bearing verbs rides the REST body as `epoch_id`, and an
// explicit value here takes precedence *above* the boot env for that one
// request — parity with the `--session` override ([Server.resolveSessionOverride]),
// differing only in that epoch is not stamped on the persisted channel-store
// row (run-isolation is enforced persona-side via the gRPC rail; the Go channel
// store keeps its `epoch_id` column default). Split into its own file (and test
// file) alongside the session-override sibling for the same cohesion reason.
package server

import (
	"context"
	"fmt"
	"strings"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// resolveEpochOverride applies an optional per-request `epoch_id` override
// (ISSUE-0085 PR 5), shared by [Server.handleChat] and
// [Server.handlePublishMessage] so the two stay in lockstep with the
// `--session` override they sit beside.
//
// When `rawEpochID` is non-blank the returned context carries
// [channels.WithEpochOverride] (so the dispatch chokepoint emits it as the
// `persatrix-epoch` header, beating the boot-resolved process epoch — PR 4's
// WithEpoch). A blank / whitespace-only value is treated as absent: the context
// is returned unchanged and the boot epoch stands, so behaviour is
// byte-identical to the pre-override path.
//
// Unlike [Server.resolveSessionOverride] it returns no effective id: the epoch
// is not stamped on the persisted channel-store row (that column keeps its
// "live" default; run-isolation is enforced persona-side via the gRPC rail), so
// there is nothing for the handler to persist — only the dispatch context to
// thread.
//
// The override id is trusted, not validated against any registry — mirroring
// the session override and the CLI's flag/env pass-through. The one shape it IS
// checked for is wire-legality: it rides the gRPC `persatrix-epoch` metadata
// header, which is printable-ASCII only. A control / non-ASCII byte would be
// rejected by the gRPC transport at send time and silently fail the dispatch
// (the publish has already returned 201), so it is rejected here with an error
// the caller surfaces as a 400 — the same fail-loud posture
// [Server.resolveSessionOverride] takes on `session_id`.
func (s *Server) resolveEpochOverride(ctx context.Context, rawEpochID string) (context.Context, error) {
	override := strings.TrimSpace(rawEpochID)
	if override == "" {
		return ctx, nil
	}
	// sessionOverrideValid is the shared gRPC-metadata printable-ASCII gate:
	// both the session and epoch overrides ride the same metadata transport, so
	// the wire-legality contract is identical. It is named for its original
	// session use (channel_session_override.go) and reused here rather than
	// duplicated.
	if !sessionOverrideValid(override) {
		return ctx, fmt.Errorf("epoch_id must be printable ASCII (no control or non-ASCII characters)")
	}
	return channels.WithEpochOverride(ctx, override), nil
}
