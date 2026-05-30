// Per-request session override — RFC 0031 Phase 3 PR 4.
//
// Split from channel_handlers.go (and shared by chat_handler.go) to keep that
// file under the 500-line review cap. The override resolution + wire-legality
// check are a cohesive concern, separable from the channel CRUD/publish verbs,
// and live alongside their test file (channel_session_override_test.go).
package server

import (
	"context"
	"fmt"
	"strings"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// resolveSessionOverride applies an optional per-request `session_id` override
// (RFC 0031 Phase 3 PR 4), shared by [Server.handleChat] and
// [Server.handlePublishMessage] so the two stay in lockstep.
//
// It returns the context to dispatch under and the session id to stamp on the
// persisted row. When `rawSessionID` is non-blank the returned context carries
// [channels.WithSessionOverride] (so the dispatch chokepoint emits it as the
// `persatrix-session` header, beating the ISSUE-0082 auto-binding) and the
// returned id is the override. A blank / whitespace-only value is treated as
// absent: the context is returned unchanged and the boot default
// ([Server.channelSessionID]) is used, so behaviour is byte-identical to the
// pre-override path.
//
// The override id is trusted, not validated against the session registry — by
// design, not omission: it mirrors the unauthenticated `sender_id` (auth lands
// in RFC 0009 Phase 4) and the CLI's env / active-session pass-through, which
// also skip the registry so an operator can name an ad-hoc or not-yet-minted
// session. On a *group* publish the override applies to every responding
// recipient of this message, not a single (agent, channel) pair.
//
// The one shape the id *is* checked for is wire-legality: it rides the gRPC
// `persatrix-session` metadata header ([grpcmeta.InjectSession]), which is
// printable-ASCII only. A control / non-ASCII byte would be rejected by the
// gRPC transport at send time and silently fail the dispatch (the publish has
// already returned 201), so a malformed value is rejected here with an error
// the caller surfaces as a 400 — consistent with the handler's fail-loud checks
// on sender_id / mention count / cascade depth.
func (s *Server) resolveSessionOverride(ctx context.Context, rawSessionID string) (context.Context, string, error) {
	override := strings.TrimSpace(rawSessionID)
	if override == "" {
		return ctx, s.channelSessionID, nil
	}
	if !sessionOverrideValid(override) {
		return ctx, "", fmt.Errorf("session_id must be printable ASCII (no control or non-ASCII characters)")
	}
	return channels.WithSessionOverride(ctx, override), override, nil
}

// sessionOverrideValid reports whether an override id is safe to ride the gRPC
// `persatrix-session` metadata header: printable ASCII only (0x20–0x7E). This
// is the gRPC/HTTP-2 metadata legal range — a byte outside it would fail the
// RPC at send time. The check is intentionally minimal (not a registry/format
// check) so legitimately ad-hoc operator ids still pass; see
// [Server.resolveSessionOverride].
func sessionOverrideValid(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 0x20 || s[i] > 0x7e {
			return false
		}
	}
	return true
}
