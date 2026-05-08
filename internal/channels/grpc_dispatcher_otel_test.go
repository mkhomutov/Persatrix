package channels

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/registry"
)

// channelsTestSpanExporter is the package-wide in-memory exporter wired
// once in TestMain. We use a single global provider for the whole test
// binary because OTEL's `otel.SetTracerProvider` has a `delegateTracerOnce`
// guard: after the first SetTracerProvider call, the package-level
// `dispatcherTracer = otel.Tracer(...)` wrapper has its delegate locked,
// and subsequent SetTracerProvider calls do NOT refresh it. Cycling
// providers per-test would silently route later tests' spans into a
// shut-down exporter. Instead, install once and `Reset()` between tests.
var channelsTestSpanExporter *tracetest.InMemoryExporter

// installSpanRecorder clears the package-wide span exporter and returns
// it so a test can inspect the spans emitted during its own dispatch
// calls. The exporter itself is initialised once in TestMain (see
// testhelpers_test.go) — read the `channelsTestSpanExporter` doc-comment
// for why per-test providers do not work.
func installSpanRecorder(t *testing.T) *tracetest.InMemoryExporter {
	t.Helper()
	if channelsTestSpanExporter == nil {
		t.Fatal("channelsTestSpanExporter is nil — TestMain did not initialise the OTEL recorder")
	}
	channelsTestSpanExporter.Reset()
	return channelsTestSpanExporter
}

// spanAttrMap collapses a recorded span stub's Attributes slice into a
// `map[string]string` so individual keys can be looked up by name in test
// assertions without iterating the slice each time. The presence-vs-absence
// distinction (used in the unknown-participant test below) relies on this
// being a true Go map, not a default-zero lookup.
func spanAttrMap(stub tracetest.SpanStub) map[string]string {
	return kvSliceToMap(stub.Attributes)
}

// kvSliceToMap collapses any `[]attribute.KeyValue` (span attributes or
// event attributes — both share the type) into a `map[string]string`.
func kvSliceToMap(kvs []attribute.KeyValue) map[string]string {
	m := make(map[string]string, len(kvs))
	for _, kv := range kvs {
		m[string(kv.Key)] = kv.Value.AsString()
	}
	return m
}

// filterSpansByName returns the subset of recorded spans whose Name
// matches `name`. Used to isolate the dispatcher's span from any
// autoinstrumentation spans the gRPC stack may add in the future.
func filterSpansByName(spans tracetest.SpanStubs, name string) tracetest.SpanStubs {
	out := make(tracetest.SpanStubs, 0, len(spans))
	for _, s := range spans {
		if s.Name == name {
			out = append(out, s)
		}
	}
	return out
}

// spanNames is a debug helper for assertion failures: when the expected
// `channel.dispatch` span is missing, the failure message lists every
// span that *was* emitted so the diagnostic is self-contained.
func spanNames(spans tracetest.SpanStubs) []string {
	names := make([]string, len(spans))
	for i, s := range spans {
		names[i] = s.Name
	}
	return names
}

// findExceptionEvent returns the first OTEL "exception" event recorded
// on the span stub (the canonical event name `RecordError` emits), or nil.
// `tracetest.SpanStub.Events` is `[]sdktrace.Event` — operate on the slice
// directly rather than threading the interface form.
func findExceptionEvent(stub tracetest.SpanStub) *sdktrace.Event {
	for i := range stub.Events {
		if stub.Events[i].Name == "exception" {
			return &stub.Events[i]
		}
	}
	return nil
}

// TestGRPCMessageDispatcher_HappyPathEmitsChannelDispatchSpan closes
// ISSUE-0032 — the production cross-process publish path relies on
// aiohttp / gRPC autoinstrumentation spans, which carry only HTTP method /
// RPC name and force operators to drill into child span attributes to
// learn which channel/recipient was involved. This test pins the
// business-logic `channel.dispatch` span shape: the parent span that
// carries `channel.id`, `channel.message_id`, `recipient.agent_id`, and
// `recipient.address` so a Jaeger / Tempo query can pivot on those keys
// directly.
func TestGRPCMessageDispatcher_HappyPathEmitsChannelDispatchSpan(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	msg := ChannelMessage{
		ID: "m-7", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	}
	require.NoError(t, d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, msg))

	spans := exporter.GetSpans()
	// The dispatcher's `channel.dispatch` span must be present. The bufconn
	// server may also produce gRPC server-side autoinstrumentation spans
	// when wired in production, but the in-process bufconn path here does
	// not — so we filter by name to keep this assertion robust to future
	// instrumentation additions rather than asserting exact span count.
	dispatchSpans := filterSpansByName(spans, "channel.dispatch")
	require.Len(t, dispatchSpans, 1,
		"exactly one channel.dispatch span must be emitted per Dispatch call; got %d (all spans: %v)",
		len(dispatchSpans), spanNames(spans))

	span := dispatchSpans[0]
	attrs := spanAttrMap(span)
	assert.Equal(t, "group:planning", attrs["channel.id"],
		"channel.id attribute missing — operators pivot dashboards on this key (ISSUE-0032)")
	assert.Equal(t, "m-7", attrs["channel.message_id"],
		"channel.message_id attribute missing — needed to correlate a span to a stored message row")
	assert.Equal(t, "agent-b", attrs["recipient.agent_id"],
		"recipient.agent_id attribute missing — needed for per-recipient delivery latency dashboards")
	assert.Equal(t, "agent-b:9090", attrs["recipient.address"],
		"recipient.address attribute missing — needed to correlate a delivery failure to a specific dial target")

	// Happy path leaves the span status Unset (the OTEL convention is that
	// only error paths flip status away from default). RecordError MUST NOT
	// have fired — no events on a successful dispatch.
	assert.Equal(t, otelcodes.Unset, span.Status.Code,
		"successful dispatch must leave status Unset; an Error/Ok flip here means a future regression has reclassified the happy path")
	assert.Empty(t, span.Events,
		"successful dispatch must emit no span events; an exception event here means RecordError fired on a path that returned nil")
}

// TestGRPCMessageDispatcher_DegradedAgentSpanRecordsError pins the
// observability contract for the "registry says this agent is degraded"
// branch. The dispatcher returns ErrAgentNotReady; the span must surface
// the failure as both `RecordError` (so trace UIs render the error event)
// and `SetStatus(Error, ...)` (so error-rate panels filter on the span
// status, not on a string scan of the events list).
func TestGRPCMessageDispatcher_DegradedAgentSpanRecordsError(t *testing.T) {
	exporter := installSpanRecorder(t)

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusDegraded},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	require.Error(t, err)
	require.ErrorIs(t, err, ErrAgentNotReady)

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	span := dispatchSpans[0]

	assert.Equal(t, otelcodes.Error, span.Status.Code,
		"degraded-agent path must set Error status so trace dashboards count it as a failure")
	require.NotEmpty(t, span.Events,
		"degraded-agent path must record the returned error as a span event (RecordError)")
	// OTEL `RecordError` emits an event named "exception" with attributes
	// `exception.type` and `exception.message`. We assert on the message
	// snippet to keep the test resilient to wrapper-error renaming.
	exc := findExceptionEvent(span)
	require.NotNil(t, exc, "expected an exception span event from RecordError")
	assert.Contains(t, kvSliceToMap(exc.Attributes)["exception.message"], "agent not ready",
		"exception event must carry the wrapped sentinel message; got %q", kvSliceToMap(exc.Attributes)["exception.message"])
}

// TestGRPCMessageDispatcher_RPCStatusErrorRecordedOnSpan pins the
// observability contract for the wire-call failure branch. A gRPC
// Unavailable from the receiver must propagate to the caller AND surface
// on the span — this is the case operators search for when the trace
// shows a successful dial followed by a failed RPC.
func TestGRPCMessageDispatcher_RPCStatusErrorRecordedOnSpan(t *testing.T) {
	exporter := installSpanRecorder(t)

	srv := &recordingAgentServer{
		respond: func() error { return status.Error(codes.Unavailable, "boom") },
	}
	dial, cleanup := startBufconnServer(t, srv)
	defer cleanup()

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{
		"agent-b": {ID: "agent-b", Address: "agent-b:9090", Status: registry.StatusHealthy},
	}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())
	d.dial = dial

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	})
	require.Error(t, err)

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1)
	span := dispatchSpans[0]

	// recipient.address must still be on the failed span — knowing which
	// dial target produced the Unavailable is the whole point of carrying
	// it (ISSUE-0032 proposed-fix line 4).
	attrs := spanAttrMap(span)
	assert.Equal(t, "agent-b:9090", attrs["recipient.address"],
		"recipient.address must be set even on RPC failure so the trace shows which target failed")

	assert.Equal(t, otelcodes.Error, span.Status.Code,
		"RPC error path must set Error status; otherwise error-rate panels under-count delivery failures")
	exc := findExceptionEvent(span)
	require.NotNil(t, exc, "expected an exception span event from RecordError")
}

// TestGRPCMessageDispatcher_UnknownParticipantSpanIsBenign pins the
// observability contract for the at-most-once silent-drop path. An
// unregistered participant returns nil from Dispatch (best-effort
// delivery — RFC 0011 §C "Delivery guarantees"). The span MUST still be
// emitted (operators need to see the drop in traces) but its status MUST
// remain Unset and no exception event must fire — flagging the drop as an
// error would inflate the orchestrator's error-rate dashboards on every
// channels.yaml typo.
//
// The recipient.address attribute is intentionally absent on this branch:
// the registry lookup returned ErrAgentNotFound before any address was
// known. Asserting absence (not "empty string") guards against a future
// regression that defaults the attribute to "" — which would silently
// pollute address-cardinality dashboards.
func TestGRPCMessageDispatcher_UnknownParticipantSpanIsBenign(t *testing.T) {
	exporter := installSpanRecorder(t)

	resolver := &stubResolver{agents: map[string]*registry.AgentInfo{}}
	d := NewGRPCMessageDispatcher(resolver, zap.NewNop())

	err := d.Dispatch(context.Background(), DispatchEnvelope{
		Recipient: Member{ParticipantID: "ghost", RespondPolicy: RespondAlways},
	}, ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
	})
	require.NoError(t, err, "unknown participant must remain a silent drop")

	dispatchSpans := filterSpansByName(exporter.GetSpans(), "channel.dispatch")
	require.Len(t, dispatchSpans, 1, "drop must still produce a trace so operators can see it")
	span := dispatchSpans[0]

	assert.Equal(t, otelcodes.Unset, span.Status.Code,
		"silent-drop path must not flag Error — RFC 0011 §C makes channel delivery best-effort, "+
			"and a typoed channels.yaml would otherwise turn every dashboard red")
	assert.Empty(t, span.Events,
		"silent-drop path must not RecordError — see status assertion above")

	attrs := spanAttrMap(span)
	assert.Equal(t, "group:planning", attrs["channel.id"])
	assert.Equal(t, "m-1", attrs["channel.message_id"])
	assert.Equal(t, "ghost", attrs["recipient.agent_id"])
	_, addressSet := attrs["recipient.address"]
	assert.False(t, addressSet,
		"recipient.address must NOT be set when the registry lookup did not yield an address — "+
			"defaulting to \"\" would pollute address-cardinality dashboards on every drop")
}
