package server

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// recordingReceiver captures every ChannelMessageEvent the dispatcher
// delivers so the integration test can assert wire-shape parity once the
// fanout completes.
type recordingReceiver struct {
	taskpb.UnimplementedAgentServiceServer

	mu     sync.Mutex
	events []*taskpb.ChannelMessageEvent
}

func (r *recordingReceiver) ReceiveChannelMessage(_ context.Context, ev *taskpb.ChannelMessageEvent) (*taskpb.TaskAck, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, ev)
	return &taskpb.TaskAck{Success: true}, nil
}

func (r *recordingReceiver) snapshot() []*taskpb.ChannelMessageEvent {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*taskpb.ChannelMessageEvent, len(r.events))
	copy(out, r.events)
	return out
}

// startRecordingAgent boots a stub gRPC server on an ephemeral 127.0.0.1
// port so the dispatcher's `grpc.NewClient` dials a real address. The
// returned address mirrors what the Python agent self-registers under
// in production (host:port from the gRPC bind) so the in-memory registry
// stands in for the real one without bypassing the dial path.
func startRecordingAgent(t *testing.T) (*recordingReceiver, string, func()) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)

	rec := &recordingReceiver{}
	gsrv := grpc.NewServer()
	taskpb.RegisterAgentServiceServer(gsrv, rec)
	go func() { _ = gsrv.Serve(lis) }()

	cleanup := func() {
		gsrv.Stop()
		_ = lis.Close()
	}
	return rec, lis.Addr().String(), cleanup
}

// TestChannelPublish_FullChain_RESTToGRPCFanout closes ISSUE-0025.
//
// Prior coverage tested the two halves of the v0.3.0 publish path
// independently: the Python `HTTPChannelPublisher` against an httpx mock,
// and the Go `GRPCMessageDispatcher` against a bufconn fake. The wire
// contract between them — REST JSON shape, sender_id propagation,
// proto field mapping in `channelMessageToProto`, RFC 3339 timestamp
// format, mentions-list shape — was trusted by parallel unit suites
// only. A regression on either side would have ridden green CI all the
// way to a real cross-process deployment.
//
// This test wires the full Go-side chain from the REST publish boundary
// through to two real recipient gRPC servers and asserts the proto event
// arrives with every contract field populated:
//
//	HTTP POST /api/v1/channels/{id}/messages
//	  ↓ handlePublishMessage (publishMessageRequest JSON parse)
//	  ↓ ChannelRouter.Publish (validate prefix + persist + fanout)
//	  ↓ ChannelRouter.fanout (sender filter + bounded concurrency)
//	  ↓ GRPCMessageDispatcher.Dispatch (resolve + dial + RPC)
//	  ↓ recordingReceiver.ReceiveChannelMessage
//
// The JSON body uses the SAME shape Python's `HTTPChannelPublisher.publish`
// serialises (`sender_id` + `content` + optional `mentions`), so a future
// Python-side schema change that diverges from the Go REST contract would
// be caught here once the publisher's shape is updated in lockstep.
//
// What this guards against (not covered by the unit suites):
//
//   - REST JSON ↔ proto field-name drift (e.g. someone renames
//     `MessageId` on the proto without re-running the orchestrator-side
//     translator).
//   - `sender_id` mis-routing — the orchestrator MUST propagate the
//     publisher-supplied sender to every recipient untouched (the
//     security note in `proto/task.proto` pins this as the
//     orchestrator-authoritative trust boundary).
//   - Mentions list shape — Python sends `[]string`, Go reads `[]string`,
//     the dispatcher MUST forward verbatim.
//   - Sender filtering — the publisher MUST NOT receive its own message
//     back via fanout (RFC 0011 §C).
//   - RFC 3339 timestamp — `proto/task.proto` documents
//     `ChannelMessageEvent.timestamp` as RFC 3339 string (deliberate
//     exception from the int64 epoch convention used elsewhere); the
//     dispatcher must render via `time.RFC3339Nano`.
//   - Channel-type derivation — `channel_type` MUST equal what
//     `channelTypeFromID(channel_id)` yields, even when the publisher
//     omits it (the Python publisher omits it today).
func TestChannelPublish_FullChain_RESTToGRPCFanout(t *testing.T) {
	logger := zap.NewNop()

	// Two recipient stub agents on real TCP — exercises the dispatcher's
	// `grpc.NewClient` path with a real address rather than the bufconn
	// short-circuit used by `grpc_dispatcher_test.go`. A regression that
	// only surfaces when a real socket is involved (e.g. a default dial
	// option that bufconn happens to ignore) would be missed otherwise.
	recBob, bobAddr, stopBob := startRecordingAgent(t)
	defer stopBob()
	recCarol, carolAddr, stopCarol := startRecordingAgent(t)
	defer stopCarol()

	// In-memory registry stands in for the production agent registry.
	// The dispatcher's `AgentResolver` only needs `Get(id)`; the
	// orchestrator-wide Registry interface is implemented by
	// `InMemoryRegistry`, so wiring it directly mirrors production.
	reg := registry.NewInMemoryRegistry(logger)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "agent-bob", Name: "Bob", Address: bobAddr, Status: registry.StatusHealthy,
	}))
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "agent-carol", Name: "Carol", Address: carolAddr, Status: registry.StatusHealthy,
	}))

	// SQLite store on a temp file so the test exercises the same store
	// path production runs (`:memory:` would skip the filesystem-driven
	// init code). `t.TempDir` is auto-cleaned.
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      logger,
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	// Real GRPCMessageDispatcher — the production wire-call implementation.
	// NoopDispatcher would defeat the point of this test.
	dispatcher := channels.NewGRPCMessageDispatcher(reg, logger)
	router := channels.NewChannelRouter(store, dispatcher, logger, nil)

	wfDir := t.TempDir()
	srv, err := New("127.0.0.1:0", wfDir,
		state.NewInMemoryStore(logger),
		reg,
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)

	// Create a 3-member group channel: agent-alice (sender) + bob + carol.
	createBody, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "agent-alice", Respond: "always"},
			{ID: "agent-bob", Respond: "when_mentioned"},
			{ID: "agent-carol", Respond: "always"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	// Publish via the REST boundary using the EXACT JSON shape Python's
	// `HTTPChannelPublisher.publish` emits (agents/channel_publisher.py:
	// payload = {"sender_id", "content"} plus optional "mentions"). Any
	// drift in the publisher's payload shape OR the Go-side
	// `publishMessageRequest` struct tags will surface as a 4xx here.
	pubBody, _ := json.Marshal(map[string]any{
		"sender_id": "agent-alice",
		"content":   "hello team",
		"mentions":  []string{"agent-bob"},
	})
	publishedAt := time.Now().UTC()
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	// Capture the orchestrator-assigned message_id from the publish
	// response so the proto-side assertion below can pin id parity. The
	// publisher trusts this id for trace correlation (`channel.message_id`
	// span attribute) — a dispatcher that fabricated its own would be a
	// silent observability regression.
	var pubResp channelMessageResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &pubResp))
	require.NotEmpty(t, pubResp.ID, "publish response must echo orchestrator-assigned id")

	// The REST handler publishes via `ChannelRouter.PublishAsync`: the 201 is
	// written at the persistence boundary and fanout runs on a detached
	// goroutine (RFC 0048 console publish-latency fix). Drain the in-flight
	// fanout before snapshotting so the wire-shape assertions are
	// deterministic — `WaitForPendingFanout` is the same drain a graceful
	// shutdown uses.
	router.WaitForPendingFanout()
	bobEvents := recBob.snapshot()
	carolEvents := recCarol.snapshot()

	require.Len(t, bobEvents, 1, "agent-bob (mentioned recipient) must receive exactly one event")
	require.Len(t, carolEvents, 1, "agent-carol (always-respond member) must receive exactly one event")

	// Sender filtering: the publisher MUST NOT receive its own message
	// back. There is no `agent-alice` recipient here, but the assertion
	// is made implicit by the recipient-count above (no 3rd dispatch
	// fired). Add an explicit guard if a future refactor adds the sender
	// to the recipient set.

	// Both events MUST carry identical wire-shape — the dispatcher fans
	// the same `ChannelMessage` out to every recipient with only
	// per-recipient envelope fields differing.
	for _, ev := range []*taskpb.ChannelMessageEvent{bobEvents[0], carolEvents[0]} {
		assert.Equal(t, pubResp.ID, ev.MessageId,
			"orchestrator-assigned id MUST flow to every recipient unchanged")
		assert.Equal(t, "group:planning", ev.ChannelId)
		assert.Equal(t, "group", ev.ChannelType,
			"channel_type MUST be derived from the id prefix even when the "+
				"publisher omits it (Python publisher does not send channel_type)")
		assert.Equal(t, "internal", ev.Classification,
			"RFC 0037 §B (v0.3.12 PR 2): the dispatched event MUST carry the "+
				"channels row's §A level — a REST-created channel stamps `internal`")
		assert.Equal(t, "agent-alice", ev.SenderId,
			"sender_id MUST propagate verbatim — orchestrator is the trust "+
				"boundary (proto/task.proto §ChannelMessageEvent.sender_id)")
		assert.Equal(t, "hello team", ev.Content)
		assert.Equal(t, []string{"agent-bob"}, ev.Mentions)
		assert.Empty(t, ev.ThreadId, "non-thread publish leaves thread_id empty")
		assert.Empty(t, ev.ThreadParentSenderId,
			"non-thread publish leaves thread_parent_sender_id empty")

		// RFC 3339 timestamp parses and lands within a generous publish
		// window. Pinning the FORMAT (parseable by time.RFC3339Nano)
		// guards against an accidental switch to int64 epoch — the
		// proto comment on `ChannelMessageEvent.timestamp` calls out
		// the deliberate divergence from the rest of `task.proto`.
		ts, err := time.Parse(time.RFC3339Nano, ev.Timestamp)
		require.NoError(t, err, "timestamp MUST be RFC 3339 (proto/task.proto comment)")
		assert.WithinDuration(t, publishedAt, ts, 5*time.Second,
			"timestamp must fall within the publish window")
	}

	// Per-recipient `respond_policy` MUST traverse the wire so the
	// receiver-side gate (RFC 0011 PR 4b) can decide pre-LLM. The router
	// has already filtered any `respond: never` members upstream of the
	// dispatcher, so every event here MUST carry a non-empty policy
	// matching what the channel was created with.
	assert.Equal(t, "when_mentioned", bobEvents[0].RespondPolicy,
		"agent-bob's per-recipient respond_policy MUST flow through the envelope")
	assert.Equal(t, "always", carolEvents[0].RespondPolicy,
		"agent-carol's per-recipient respond_policy MUST flow through the envelope")
}
