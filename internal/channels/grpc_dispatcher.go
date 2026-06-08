package channels

import (
	"context"
	"errors"
	"fmt"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/observability/grpcmeta"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// dispatcherTracer emits the orchestrator's `channel.dispatch` business-
// logic span (ISSUE-0032). The autoinstrumentation gRPC client/server
// spans already correlate the wire hops, but their names carry only the
// RPC method — operators querying for "all spans for channel X" or
// "deliveries to recipient Y" need a parent span with attributes pinned
// to the publish-path vocabulary (`channel.id`, `recipient.agent_id`,
// `recipient.address`, `channel.message_id`). Naming follows the bare
// component-namespaced convention documented in
// [docs/observability.md §10.1] and matches the existing
// [internal/executor.executorTracer] / [internal/server.chatHandlerTracer]
// shape.
var dispatcherTracer = otel.Tracer("persatrix/channels/dispatch")

// AgentResolver is the subset of [registry.Registry] that
// [GRPCMessageDispatcher] needs to translate a participant id into a dialable
// gRPC address. Defined locally so the channels package does not take a
// dependency on the full Registry surface (List/UpdateStatus/etc.) — only
// `Get` is required for per-recipient lookups.
type AgentResolver interface {
	Get(ctx context.Context, agentID string) (*registry.AgentInfo, error)
}

// SessionBinder resolves (and on first sight mints + persists) the
// per-request session id for the `(agent, channel)` unit — room continuity,
// per the RFC 0031 §A scope-axes amendment (ISSUE-0083 dropped the sender
// axis). [*SessionResolver] is the production implementation; the interface
// is the seam unit tests stub so dispatch coverage does not need a real
// SQLite store. Defined locally (not imported) so the dispatcher depends only
// on the one method it calls.
type SessionBinder interface {
	Resolve(ctx context.Context, agentID, channelID string) (string, error)
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
	// sessions resolves the per-request `persatrix-session` id emitted on
	// the outbound gRPC metadata (ISSUE-0082 PR 2). Nil on the
	// channels-disabled / NoopDispatcher-sibling paths and in tests that do
	// not exercise emission — a nil binder means no session header, so
	// behaviour is byte-identical to the pre-ISSUE-0082 dispatch.
	sessions SessionBinder
	// epoch is the per-process run/test-isolation id emitted on the outbound
	// `persatrix-epoch` gRPC metadata on every dispatch (ISSUE-0085 PR 4).
	// Unlike `sessions` (resolved per request) this is a single value the
	// orchestrator resolves once at boot from PERSATRIX_EPOCH. Empty on the
	// channels-disabled / pre-wiring paths and in tests that do not exercise
	// emission — an empty epoch means no header, so behaviour is
	// byte-identical to the pre-ISSUE-0085 dispatch.
	epoch string
}

// DispatcherOption configures a [GRPCMessageDispatcher] at construction.
type DispatcherOption func(*GRPCMessageDispatcher)

// WithSessionResolver wires the per-request [SessionBinder] whose id is
// emitted as the `persatrix-session` gRPC header on every dispatch. Omitting
// it leaves the dispatcher emitting no session header (the pre-ISSUE-0082
// behaviour).
func WithSessionResolver(b SessionBinder) DispatcherOption {
	return func(d *GRPCMessageDispatcher) { d.sessions = b }
}

// WithEpoch wires the per-process run/test-isolation epoch (resolved once at
// orchestrator boot from PERSATRIX_EPOCH) emitted as the `persatrix-epoch`
// gRPC header on every dispatch (ISSUE-0085 PR 4). Omitting it (or passing an
// empty id) leaves the dispatcher emitting no epoch header — the
// pre-ISSUE-0085 behaviour.
func WithEpoch(epoch string) DispatcherOption {
	return func(d *GRPCMessageDispatcher) { d.epoch = epoch }
}

// ErrAgentNotReady is returned (wrapped) when the registry reports a
// participant in a non-`StatusHealthy` state. Callers (the router) record
// `status="error"` and do not retry.
var ErrAgentNotReady = errors.New("channels: agent not ready")

// NewGRPCMessageDispatcher wires a dispatcher around the orchestrator's
// agent registry. `logger` may be nil — replaced with `zap.NewNop()` to
// keep the no-OTEL test paths quiet.
func NewGRPCMessageDispatcher(resolver AgentResolver, logger *zap.Logger, opts ...DispatcherOption) *GRPCMessageDispatcher {
	if logger == nil {
		logger = zap.NewNop()
	}
	d := &GRPCMessageDispatcher{
		resolver: resolver,
		logger:   logger,
		dial:     grpc.NewClient,
	}
	for _, opt := range opts {
		opt(d)
	}
	return d
}

// Dispatch implements [MessageDispatcher].
func (d *GRPCMessageDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	participantID := env.Recipient.ParticipantID

	// ISSUE-0032: emit the business-logic `channel.dispatch` span before
	// any work so even the silent-drop path (unknown participant) shows
	// up in traces. `recipient.address` is intentionally NOT set here —
	// the registry lookup may fail, and defaulting to "" would pollute
	// address-cardinality dashboards on every channels.yaml typo. We
	// set it later, only after the resolver yields a real address.
	ctx, span := dispatcherTracer.Start(ctx, "channel.dispatch",
		trace.WithAttributes(
			attribute.String("channel.id", msg.ChannelID),
			attribute.String("channel.message_id", msg.ID),
			attribute.String("recipient.agent_id", participantID),
		),
	)
	defer span.End()

	agent, err := d.resolver.Get(ctx, participantID)
	if err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			// Best-effort, at-most-once: a participant who is not
			// registered yet picks the message up on reconnect via
			// the history endpoint. Logged at warn (not error) so a
			// reasonable channels.yaml with one mistyped membership
			// does not turn the operator's logs red.
			//
			// Span: leave status Unset and do NOT RecordError —
			// flagging this branch as Error would inflate orchestrator
			// error-rate dashboards on every typo (RFC 0011 §C
			// "Delivery guarantees" makes this best-effort).
			d.logger.Warn("channels: dispatch target not registered; dropping (read via history on reconnect)",
				zap.String("participant_id", participantID),
				zap.String("channel_id", msg.ChannelID),
				zap.String("message_id", msg.ID),
			)
			return nil
		}
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return fmt.Errorf("registry lookup for %s: %w", participantID, err)
	}
	if agent.Status != registry.StatusHealthy {
		err := fmt.Errorf("%w: %s status=%s", ErrAgentNotReady, participantID, agent.Status)
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return err
	}
	if agent.Address == "" {
		err := fmt.Errorf("%w: %s has empty address", ErrAgentNotReady, participantID)
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return err
	}

	// Address known — pin it on the span so a delivery failure can be
	// correlated to a specific dial target by Jaeger/Tempo query.
	span.SetAttributes(attribute.String("recipient.address", agent.Address))

	// ISSUE-0082 PR 2: resolve the per-request session for the
	// (recipient-agent, channel) unit — room continuity, per the RFC 0031 §A
	// scope-axes amendment (ISSUE-0083 dropped the sender axis: co-speakers in
	// one room now share the agent's session rather than fragmenting it) — and
	// emit it as the `persatrix-session` gRPC header, feeding the ISSUE-0081
	// rail that re-establishes a per-room `session_scope` persona-side. The
	// resolver is the single source of the id; `Dispatch` is the only live
	// emission site (the synchronous chat path is dead-but-wired, ISSUE-0035).
	//
	// RFC 0031 Phase 3 PR 4: an explicit per-request `--session` override
	// (threaded onto ctx by the REST handler — see [WithSessionOverride]) is
	// the highest-precedence signal (OQ #6 amendment). When present it beats
	// the auto-binding for this one request — the reconciliation that lets an
	// operator deliberately re-bind a conversation (e.g. a dementia-test arc
	// across runs, RFC 0031 OQ #1 resolution 1a). It is checked *before* the
	// resolver, so an override does not even consult the binder; absent an
	// override the auto-binding stands and concurrent-isolation is unchanged.
	//
	// Resolution failure is non-fatal by design: a session hiccup must never
	// drop a message. On error we log and dispatch without the header, so the
	// persona falls back to its construction-time (legacy) snapshot — exactly
	// the pre-activation behaviour, with no row stranded (§D legacy carve-out).
	//
	// `msg.SenderID` is intentionally not part of the session key (ISSUE-0083);
	// it stays available for the per-participant relationship / facts write
	// paths, which are correctly sender-scoped.
	if override := SessionOverrideFromContext(ctx); override != "" {
		ctx = grpcmeta.InjectSession(ctx, override)
		span.SetAttributes(attribute.String("session.id", override))
	} else if d.sessions != nil {
		sid, sErr := d.sessions.Resolve(ctx, participantID, msg.ChannelID)
		switch {
		case sErr != nil:
			d.logger.Warn("channels: session resolve failed; dispatching without persatrix-session (persona falls back to legacy snapshot)",
				zap.String("participant_id", participantID),
				zap.String("channel_id", msg.ChannelID),
				zap.Error(sErr),
			)
		case sid != "":
			ctx = grpcmeta.InjectSession(ctx, sid)
			// Low-cardinality-on-span, never a metric label (RFC 0031 OQ #7):
			// pin the session so a trace can be pivoted to the conversation
			// it served.
			span.SetAttributes(attribute.String("session.id", sid))
		}
	}

	// ISSUE-0085 PR 4: emit the per-process run/test-isolation epoch on every
	// dispatch so the persona side re-establishes an `epoch_scope` for the
	// strict-equality run-isolation filter. Unlike the session id, the epoch is
	// process-global (resolved once at boot, not per-room), so there is no
	// resolver and no per-request failure path.
	//
	// ISSUE-0085 PR 5: an explicit per-request `--epoch` override (threaded
	// onto ctx by the REST handler — see [WithEpochOverride]) takes precedence
	// *above* the boot epoch for the one request it accompanies, mirroring the
	// `--session` override above. Absent an override the boot epoch ([WithEpoch])
	// stands, so the process-global default (PR 4) is byte-identically
	// preserved. When neither is present nothing is emitted (channels-disabled /
	// pre-wiring path) and the persona falls back to its construction-time
	// ("live") snapshot, byte-identical to pre-ISSUE-0085.
	if override := EpochOverrideFromContext(ctx); override != "" {
		ctx = grpcmeta.InjectEpoch(ctx, override)
		span.SetAttributes(attribute.String("epoch.id", override))
	} else if d.epoch != "" {
		ctx = grpcmeta.InjectEpoch(ctx, d.epoch)
		// Low-cardinality-on-span, never a metric label (RFC 0031 OQ #7),
		// matching the session-id posture: pin the epoch so a trace can be
		// pivoted to the logical run / branch it served.
		span.SetAttributes(attribute.String("epoch.id", d.epoch))
	}

	conn, err := d.dial(agent.Address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
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
	event := d.channelMessageToProto(msg, env)
	if _, err := client.ReceiveChannelMessage(ctx, event); err != nil {
		// Wire-call failure: surface on the span so an operator searching
		// by trace_id sees the receiver's gRPC status code attached to
		// the parent span rather than only on the autoinstrumentation
		// child.
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
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
//
// RFC 0011 PR 4b: `respond_policy` and `thread_parent_sender_id` are
// pulled from the [DispatchEnvelope] so the receiver's response gate
// can decide pre-LLM without a secondary REST roundtrip. The router
// guarantees `env.Recipient.RespondPolicy` is one of [RespondAlways] or
// [RespondWhenMentioned] — `respond: never` members are filtered out
// upstream of [MessageDispatcher.Dispatch].
func (d *GRPCMessageDispatcher) channelMessageToProto(msg ChannelMessage, env DispatchEnvelope) *taskpb.ChannelMessageEvent {
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
		MessageId:            msg.ID,
		ChannelId:            msg.ChannelID,
		ChannelType:          string(ct),
		SenderId:             msg.SenderID,
		Content:              msg.Content,
		Timestamp:            ts.UTC().Format(time.RFC3339Nano),
		ThreadId:             msg.ThreadID,
		Mentions:             msg.Mentions,
		RespondPolicy:        string(env.Recipient.RespondPolicy),
		ThreadParentSenderId: env.ThreadParentSenderID,
		// [RFC 0011 amendment 'Cascade-depth wire propagation']: the
		// router's Publish clamped `msg.Metadata["cascade_depth"]` to
		// `[0, maxCascadeDepth]` before persistence, so the int32
		// downcast cannot overflow on a misbehaving publisher. proto3
		// scalars zero-value to 0, which is exactly the chain-origin
		// semantic for a publish that omits the field.
		//
		// [RFC 0011 amendment 'Cascade-depth wire propagation']: ../../docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
		CascadeDepth: int32(readCascadeDepth(msg.Metadata)),
		// ISSUE-0068 / [RFC 0011 amendment 'Participant-type wire
		// propagation']: lift the publish-side `participant_type`
		// (set by the REST chat handler) onto the typed proto field so
		// the peer type survives this boundary. Empty for ordinary
		// agent-to-agent fanout — the agent resolves that to "agent".
		//
		// [RFC 0011 amendment 'Participant-type wire propagation']: ../../docs/rfcs/0011-amendment-participant-type-wire-propagation.md
		SenderParticipantType: readParticipantType(msg.Metadata),
		// RFC 0030 Tier B (v0.3.8): the per-recipient salience-bid inputs. The
		// bid-ness + threshold ride on the recipient's membership row
		// (resolved at config load / REST add); the channel size + cap are
		// per-publish values the router stamped on the envelope at fanout.
		// `Threshold` is `*float64` → the optional proto field: nil leaves it
		// absent (the agent reads "unset → bias-to-silence"), distinct from an
		// explicit 0.0.
		SalienceGated:             env.Recipient.SalienceGated,
		Threshold:                 env.Recipient.Threshold,
		ChannelSize:               int32(env.ChannelSize),
		SalienceMaxChannelMembers: int32(env.SalienceMaxChannelMembers),
		// RFC 0030 deterministic governance layers (v0.3.8), PR 1: lift any
		// publish-side `interaction_id` (the RFC 0020 Interaction — lifecycle
		// §B/§C, per-channel scope §G) onto the typed proto field so Layers
		// 1/2/4 can attribute spend, count replies, and accumulate end-votes
		// per interaction. Empty for an untracked publish — every layer stays
		// at its uncapped default, so the field is additive. Inert this PR:
		// no layer reads it yet, and no producer writes the metadata key yet
		// (interaction tracking is agent-side; see interaction_id.go), so
		// readInteractionID returns "" on every publish today.
		//
		// [RFC 0030 governance layers]: ../../docs/rfcs/0030-governance-layers-pr-plan.md
		InteractionId: readInteractionID(msg.Metadata),
	}
}
