package channels

import (
	"context"
	"errors"
	"fmt"

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
//     and returned as a WRAPPED error. RFC 0011 §C "Delivery guarantees"
//     makes channel delivery at-most-once and strictly best-effort — a
//     participant who is not registered yet (think: a user who has not
//     connected, or an agent restarting) reads the message via the history
//     endpoint when they come online, and no caller retries — but the miss
//     must be VISIBLE to the caller: the bounded-close undelivered ledger
//     ([liveDeliveryFailures]) records exactly the members whose live
//     delivery failed, and the old tolerant nil recorded such a miss as a
//     live delivery — the member was then stamped
//     `close_notification_redelivery=true` and its agent-side ingest skip
//     dropped the closing turn from its record permanently (PR #718
//     review). A caller that deliberately fires at possibly-absent
//     participants can branch on `errors.Is(err,
//     registry.ErrAgentNotFound)`; [ChannelRouter.dispatchTo] does exactly
//     that for its delivered counter (status="unregistered", not "error",
//     so a standing human member does not redden a healthy channel's
//     dashboards message after message).
//   - A receiver ack with `success=false` is returned as an error for the
//     same reason: the agent servicer's queue-full discard-not-block
//     backpressure and its pre-ingest validation both ack the RPC while
//     refusing the event (agents/server_servicers.py), so a transport-level
//     nil is NOT a delivery.
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

// ErrDeliveryRefused is returned (wrapped) when the receiver ACKED the RPC but
// REFUSED the event — the agent servicer's queue-full discard-not-block
// backpressure and its pre-ingest validation both take this shape (see the
// struct doc). A sentinel, not a bare fmt.Errorf, so the one synchronous
// dispatch-returning endpoint (POST /convene) can map this routine,
// retryable miss to a truthful 503 instead of the default 500 "channel store
// error" + Error-level "unexpected error" log (PR #718 review) — the fanout
// paths never surface it to HTTP, so they are unaffected.
var ErrDeliveryRefused = errors.New("receiver refused delivery")

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
			// the history endpoint, and no caller retries. Logged at
			// warn (not error) so a reasonable channels.yaml with one
			// mistyped membership does not turn the operator's logs
			// red — but RETURNED as an error (wrapped, so errors.Is
			// still discriminates): the undelivered ledger keys the
			// close-notification redelivery marker on this return, and
			// the old tolerant nil recorded the miss as a live delivery
			// — the member's ingest skip then dropped its closing turn
			// permanently (PR #718 review; see the struct doc).
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
			return fmt.Errorf("dispatch target %s not registered: %w", participantID, err)
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

	// ISSUE-0082 Part 2 PR 1 (v0.3.14): emit the per-request verified
	// principal — the tenant axis beside session and epoch — as the
	// `persatrix-principal` header, feeding the strict-equality principal
	// rail persona-side. Unlike the two axes above there is no resolver and
	// no boot default: the request context is the only source
	// ([WithPrincipal], stamped by the REST handlers under
	// `auth.mode: enabled` once PR 2 lands the producer). Absent a value
	// nothing is emitted and the persona resolves its 'local' default —
	// the normal path for `disabled` mode, unauthenticated callers, and
	// every agent/autonomous-origin turn, byte-identical to pre-activation
	// behaviour. Dormant in this PR: no production code stamps the ctx yet.
	if principal := PrincipalFromContext(ctx); principal != "" {
		ctx = grpcmeta.InjectPrincipal(ctx, principal)
		// Low-cardinality-on-span, never a metric label (RFC 0031 OQ #7),
		// matching the session/epoch posture: pin the principal so a trace
		// can be pivoted to the authenticated person it served — the
		// provenance signal the v0.3.14 live MT reads.
		span.SetAttributes(attribute.String("principal.id", principal))
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
	ack, err := client.ReceiveChannelMessage(ctx, event)
	if err != nil {
		// Wire-call failure: surface on the span so an operator searching
		// by trace_id sees the receiver's gRPC status code attached to
		// the parent span rather than only on the autoinstrumentation
		// child.
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		return fmt.Errorf("ReceiveChannelMessage to %s: %w", participantID, err)
	}
	if !ack.GetSuccess() {
		// The receiver ACKED the RPC but REFUSED the event — the agent
		// servicer's queue-full discard-not-block backpressure and its
		// pre-ingest validation both take this shape
		// (agents/server_servicers.py). Discarding the ack body made those
		// misses indistinguishable from deliveries, so the undelivered
		// ledger under-recorded them (the same closing-turn loss as the
		// unregistered branch above — PR #718 review). GetSuccess is
		// nil-tolerant, so a degenerate nil-ack/nil-err reply also lands
		// here rather than passing as delivered.
		ackErr := fmt.Errorf("ReceiveChannelMessage to %s: %w: %s", participantID, ErrDeliveryRefused, ack.GetErrorMessage())
		span.RecordError(ackErr)
		span.SetStatus(otelcodes.Error, ackErr.Error())
		return ackErr
	}
	return nil
}
