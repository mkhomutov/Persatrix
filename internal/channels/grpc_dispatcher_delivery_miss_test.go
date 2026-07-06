package channels

// grpc_dispatcher_delivery_miss_test.go — PR #718 review: the two delivery
// misses [GRPCMessageDispatcher.Dispatch] used to nil-swallow. An unregistered
// target returned nil ("read via history on reconnect") and the TaskAck body
// was discarded, so a receiver ack with success=false (the agent servicer's
// queue-full discard-not-block backpressure, or its pre-ingest validation)
// also read as delivered. The undelivered ledger records only on a non-nil
// Dispatch error, so both shapes were stamped
// `close_notification_redelivery=true` on a floor-path bounded close and the
// member's ingest skip dropped its closing turn permanently. Both must now
// surface as errors — the unregistered miss WRAPPED, so callers that ever
// need the old tolerance can errors.Is on [registry.ErrAgentNotFound].

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

func TestGRPCMessageDispatcher_UnknownParticipantReturnsWrappedNotFound(t *testing.T) {
	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "ghost", RespondPolicy: RespondWhenMentioned},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	require.Error(t, err,
		"an unregistered target is a delivery MISS — a nil here records it as a live delivery in the undelivered ledger")
	assert.ErrorIs(t, err, registry.ErrAgentNotFound,
		"wrapped, not flattened: a caller that deliberately fires at possibly-absent targets must be able to errors.Is it")
}

// refusingAckServer acks every ReceiveChannelMessage RPC at the transport
// level while refusing the event in the TaskAck body — the wire shape of the
// agent servicer's queue-full backpressure and validation failures.
type refusingAckServer struct {
	taskpb.UnimplementedAgentServiceServer
}

func (refusingAckServer) ReceiveChannelMessage(context.Context, *taskpb.ChannelMessageEvent) (*taskpb.TaskAck, error) {
	return &taskpb.TaskAck{Success: false, ErrorMessage: "event queue full; message discarded"}, nil
}

func TestGRPCMessageDispatcher_RefusedAckReturnsError(t *testing.T) {
	dial, cleanup := startBufconnServer(t, refusingAckServer{})
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "ignored:0", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	})
	require.Error(t, err,
		"success=false in the ack body is a refused delivery; nil here let the router record status=\"ok\" against a discarded message")
	assert.Contains(t, err.Error(), "receiver refused delivery",
		"the error names the refusal shape so a warn log distinguishes it from a wire failure")
	assert.Contains(t, err.Error(), "event queue full",
		"the receiver's own reason rides the error for the operator")
}
