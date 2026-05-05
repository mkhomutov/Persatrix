package channels

import (
	"context"
	"errors"
	"fmt"
	"time"

	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// AgentResolver is the subset of [registry.Registry] that
// [GRPCMessageDispatcher] needs to translate a participant id into a dialable
// gRPC address. Defined locally so the channels package does not take a
// dependency on the full Registry surface (List/UpdateStatus/etc.) — only
// `Get` is required for per-recipient lookups.
type AgentResolver interface {
	Get(ctx context.Context, agentID string) (*registry.AgentInfo, error)
}

// dialFunc is the seam the dispatcher uses to open a gRPC connection.
// Replaceable in tests so unit coverage can stub the wire without spinning
// up a real gRPC server.
type dialFunc func(target string, opts ...grpc.DialOption) (*grpc.ClientConn, error)

// GRPCMessageDispatcher is the production [MessageDispatcher] introduced in
// RFC 0011 PR 4a-ii-β-1. It replaces [NoopDispatcher] in the orchestrator
// startup path and turns each per-recipient `Dispatch` call into an
// `AgentService.ReceiveChannelMessage` gRPC invocation against the address
// the participant registered with under [registry.AgentInfo.Address].
//
// Behavior contract:
//
//   - Sender filtering, member lookup, and `respond: never` short-circuit
//     all happen in [ChannelRouter.fanout] upstream — by the time `Dispatch`
//     fires here the recipient is guaranteed eligible. The dispatcher's only
//     job is the wire call.
//   - Unknown participants ([registry.ErrAgentNotFound]) are logged at warn
//     and treated as a no-op. RFC 0011 §C "Delivery guarantees" makes
//     channel delivery at-most-once and strictly best-effort — a participant
//     who is not registered yet (think: a user who has not connected, or an
//     agent restarting) reads the message via the history endpoint when
//     they come online.
//   - Offline / degraded agents: the registry pre-filters to `StatusHealthy`.
//     Anything else returns an error wrapped around [ErrAgentNotReady] so
//     the router metrics record `status="error"` without retrying.
//   - One gRPC connection is opened per dispatch. PR 4a-ii-β-1 keeps the
//     dial path simple (no pooling) — the same trade-off
//     [internal/executor.dispatch] makes today. The "TODO(v0.2): connection
//     pooling" comment there now applies to both call sites; consolidating
//     is tracked as a v0.4.0 follow-up so this PR stays sized for review.
//   - Per-call timeout: the caller's context already carries the 5s
//     per-recipient deadline from [ChannelRouter.fanout]. The dispatcher
//     does not stack a second timeout on top.
//
// Security / trust boundary:
//
//   - The wire RPC carries the orchestrator-authoritative `sender_id`
//     populated by [ChannelRouter.Publish] (which itself takes the value
//     from the REST handler's framework-injected sender, never the
//     untrusted body). The dispatcher does not re-derive or override
//     `SenderId` — it only ferries the already-validated message.
//   - Transport credentials are `insecure` in v0.3.0 because the agent
//     gRPC port is documented as cleartext / local-only. mTLS lands with
//     RFC 0009 Phase 4; same `TODO(security)` marker as
//     [internal/executor/dispatch.go] dial path so a single grep finds
//     both call sites when the upgrade lands.
type GRPCMessageDispatcher struct {
	resolver AgentResolver
	logger   *zap.Logger
	dial     dialFunc
}

// ErrAgentNotReady is returned (wrapped) when the registry reports a
// participant in a non-`StatusHealthy` state. Callers (the router) record
// `status="error"` and do not retry.
var ErrAgentNotReady = errors.New("channels: agent not ready")

// NewGRPCMessageDispatcher wires a dispatcher around the orchestrator's
// agent registry. `logger` may be nil — replaced with `zap.NewNop()` to
// keep the no-OTEL test paths quiet.
func NewGRPCMessageDispatcher(resolver AgentResolver, logger *zap.Logger) *GRPCMessageDispatcher {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &GRPCMessageDispatcher{
		resolver: resolver,
		logger:   logger,
		dial:     grpc.NewClient,
	}
}

// Dispatch implements [MessageDispatcher].
func (d *GRPCMessageDispatcher) Dispatch(ctx context.Context, participantID string, msg ChannelMessage) error {
	agent, err := d.resolver.Get(ctx, participantID)
	if err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			// Best-effort, at-most-once: a participant who is not
			// registered yet picks the message up on reconnect via
			// the history endpoint. Logged at warn (not error) so a
			// reasonable channels.yaml with one mistyped membership
			// does not turn the operator's logs red.
			d.logger.Warn("channels: dispatch target not registered; dropping (read via history on reconnect)",
				zap.String("participant_id", participantID),
				zap.String("channel_id", msg.ChannelID),
				zap.String("message_id", msg.ID),
			)
			return nil
		}
		return fmt.Errorf("registry lookup for %s: %w", participantID, err)
	}
	if agent.Status != registry.StatusHealthy {
		return fmt.Errorf("%w: %s status=%s", ErrAgentNotReady, participantID, agent.Status)
	}
	if agent.Address == "" {
		return fmt.Errorf("%w: %s has empty address", ErrAgentNotReady, participantID)
	}

	conn, err := d.dial(agent.Address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return fmt.Errorf("dial %s at %s: %w", participantID, agent.Address, err)
	}
	// PR #250 review (Should-Fix #3): the prior `defer conn.Close()`
	// silently discarded any close error. With one connection per
	// dispatch (no pooling — see struct doc) a half-open cleanup issue
	// would otherwise be invisible until file-descriptor exhaustion
	// surfaced it elsewhere. Log at debug-level to avoid noise on the
	// happy path while still leaving a breadcrumb when the underlying
	// transport reports a close error.
	defer func() {
		if cerr := conn.Close(); cerr != nil {
			d.logger.Warn("channels: gRPC connection close returned error",
				zap.String("participant_id", participantID),
				zap.String("address", agent.Address),
				zap.Error(cerr),
			)
		}
	}()

	client := taskpb.NewAgentServiceClient(conn)
	event := d.channelMessageToProto(msg)
	if _, err := client.ReceiveChannelMessage(ctx, event); err != nil {
		return fmt.Errorf("ReceiveChannelMessage to %s: %w", participantID, err)
	}
	return nil
}

// channelMessageToProto translates the in-process [ChannelMessage] into the
// wire [taskpb.ChannelMessageEvent]. Timestamp is rendered as RFC 3339 to
// match the proto field's documented contract (see PR #246 deep review M4).
//
// `ChannelType` is re-derived from the channel id prefix here rather than
// added as a struct field — keeping [ChannelMessage] free of a denormalized
// type column matches what the SQLite store persists. The router has already
// validated the prefix once on the publish path, so an unknown prefix at
// dispatch time is a programmer error.
//
// PR #250 review (Medium #4): a translation-time Warn surfaces that
// programmer error at the sender's logs the moment it happens, rather
// than letting an empty `ChannelType` ride the wire to the receiver
// where the origin is opaque. The contract — empty string on unknown
// prefix — is preserved so the receiver's proto-bound validation still
// rejects the message.
func (d *GRPCMessageDispatcher) channelMessageToProto(msg ChannelMessage) *taskpb.ChannelMessageEvent {
	ct, ctErr := channelTypeFromID(msg.ChannelID)
	if ctErr != nil {
		d.logger.Warn("channels: unknown channel_id prefix at dispatch translation; sending empty ChannelType (router prefix validation regression?)",
			zap.String("channel_id", msg.ChannelID),
			zap.String("message_id", msg.ID),
			zap.Error(ctErr),
		)
	}
	ts := msg.Timestamp
	if ts.IsZero() {
		ts = time.Now().UTC()
	}
	return &taskpb.ChannelMessageEvent{
		MessageId:   msg.ID,
		ChannelId:   msg.ChannelID,
		ChannelType: string(ct),
		SenderId:    msg.SenderID,
		Content:     msg.Content,
		Timestamp:   ts.UTC().Format(time.RFC3339Nano),
		ThreadId:    msg.ThreadID,
		Mentions:    msg.Mentions,
	}
}
