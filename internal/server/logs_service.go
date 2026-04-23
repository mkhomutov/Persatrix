// Package server — RFC 0018 PR 5: orchestrator-side LogService.
//
// Receives streamed log batches from agents (and any other LogService
// client), validates each entry, and admits it to the orchestrator's
// per-execution ring buffer.  Sends periodic LogAck messages so the
// shipper can advance its in-memory high-water mark and free already-
// acked entries.
package server

import (
	"errors"
	"io"

	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/generated/logpb"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// maxEntriesPerBatch caps how many LogEntries a single LogBatch may
// carry before the server rejects the batch with InvalidArgument.
// Pinned at 1024 per RFC 0018 § E and the proto file's TODO comment;
// the per-execution rate limiter is per-entry so a single oversized
// batch would otherwise bypass it.
const maxEntriesPerBatch = 1024

// ackEveryN is the LogAck cadence: send one ack per N admitted
// entries.  Keeps the bidi stream lively (the shipper uses the ack to
// free its in-memory queue) without flooding it.  RFC 0018 § E does not
// pin the value; 32 matches typical batched-shipper sizing.
const ackEveryN = 32

// LogServiceServer is the gRPC LogService implementation registered on
// the orchestrator's agent-facing gRPC server.  Wraps a Buffer so
// Append's deny-by-default semantics apply uniformly to wire ingest
// and any in-process call sites that may exist later.
type LogServiceServer struct {
	logpb.UnimplementedLogServiceServer
	buf    *logbuffer.Buffer
	logger *zap.Logger
}

// NewLogServiceServer constructs a LogServiceServer.  buf must be
// non-nil — the constructor panics otherwise so a misuse is caught at
// startup rather than as a nil-pointer panic on the first inbound
// stream.
func NewLogServiceServer(buf *logbuffer.Buffer, logger *zap.Logger) *LogServiceServer {
	if buf == nil {
		panic("server.NewLogServiceServer: buf is nil")
	}
	if logger == nil {
		logger = zap.NewNop()
	}
	return &LogServiceServer{buf: buf, logger: logger}
}

// StreamLogs is the bidirectional stream handler.  Per the RFC, the
// transport contract is:
//   - The client (agent shipper) sends LogBatch messages.
//   - The server (orchestrator) sends LogAck messages whose
//     received_through_seq is a monotonically-increasing per-stream
//     sequence number reflecting how many entries the server has
//     accepted.
//   - On any drop the entry is counted in Buffer's drop counters but
//     the stream continues — drops are deliberately silent on the wire
//     so a noisy execution does not produce per-entry NAK chatter.
//   - On a malformed batch (entries == nil and agent_id == "", or
//     more than maxEntriesPerBatch entries) the stream is terminated
//     with InvalidArgument so a buggy shipper fails fast.
func (s *LogServiceServer) StreamLogs(stream logpb.LogService_StreamLogsServer) error {
	var receivedThroughSeq uint64
	var sinceLastAck uint64
	for {
		batch, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			// Final ack so the shipper can free its tail before
			// closing the stream.  Ignore any send error: the peer
			// is already gone.
			_ = stream.Send(&logpb.LogAck{ReceivedThroughSeq: receivedThroughSeq})
			return nil
		}
		if err != nil {
			s.logger.Debug("logservice: stream recv error",
				zap.Error(err),
				zap.Uint64("received_through_seq", receivedThroughSeq),
			)
			return err
		}
		entries := batch.GetEntries()
		if len(entries) > maxEntriesPerBatch {
			return status.Errorf(codes.InvalidArgument,
				"logservice: batch entries %d exceeds cap %d",
				len(entries), maxEntriesPerBatch)
		}
		batchAgent := batch.GetAgentId()
		for _, pe := range entries {
			if pe == nil {
				continue
			}
			entry := protoToEntry(pe, batchAgent)
			// Append's deny-by-default precedence (closed → no
			// exec id → invalid id → below level → rate limit) is
			// the single boundary; we do not duplicate it here.
			s.buf.Append(entry)
			receivedThroughSeq++
			sinceLastAck++
		}
		if sinceLastAck >= ackEveryN {
			if err := stream.Send(&logpb.LogAck{ReceivedThroughSeq: receivedThroughSeq}); err != nil {
				return err
			}
			sinceLastAck = 0
		}
	}
}

// protoToEntry maps the wire LogEntry to the in-memory logbuffer.Entry.
// The conversion is intentionally tolerant: missing optional fields
// become zero values which the encoder downstream will omit.  The
// schema_version on the wire is preserved verbatim — the orchestrator
// does not rewrite it because doing so would mask a future agent
// emitting a newer schema.
//
// agent_id resolution: per-entry agent_id wins (matches the proto
// file's documented precedence); batch_agent is used only when the
// per-entry value is empty.  This lets a shipper batch entries from
// multiple agents in a single stream without per-entry repetition for
// the common single-agent case.
func protoToEntry(pe *logpb.LogEntry, batchAgent string) logbuffer.Entry {
	agentID := pe.GetAgentId()
	if agentID == "" {
		agentID = batchAgent
	}
	e := logbuffer.Entry{
		SchemaVersion:   pe.GetSchemaVersion(),
		Level:           pe.GetLevel(),
		ServiceKind:     pe.GetServiceKind(),
		ServiceInstance: pe.GetServiceInstance(),
		ServiceRole:     pe.GetServiceRole(),
		Message:         pe.GetMessage(),
		ExecutionID:     pe.GetExecutionId(),
		StepID:          pe.GetStepId(),
		AgentID:         agentID,
		RequestID:       pe.GetRequestId(),
		TraceID:         pe.GetTraceId(),
		SpanID:          pe.GetSpanId(),
	}
	if ts := pe.GetTimestamp(); ts != nil {
		e.Timestamp = ts.AsTime()
	}
	if attrs := pe.GetAttributes(); attrs != nil {
		e.Attributes = attrs.AsMap()
	}
	if src := pe.GetSource(); src != nil {
		e.Source = &logbuffer.Source{
			File:     src.GetFile(),
			Line:     src.GetLine(),
			Function: src.GetFunction(),
		}
	}
	return e
}
