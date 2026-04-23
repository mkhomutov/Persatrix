// Package grpcmeta carries the four Persatrix correlation IDs across the
// orchestrator → agent gRPC boundary (RFC 0018 Phase 3 / § D).
//
// Two pieces of state are propagated on every outbound RPC:
//
//   - the W3C TraceContext + Baggage that RFC 0019 Phase 1 already wires via
//     the otelgrpc client/server interceptor (handled elsewhere); and
//   - this package's four kebab-case metadata keys that bind structured-log
//     correlation IDs onto the agent-side handler context.
//
// The two are deliberately independent: the OTEL trace IDs let an operator
// pivot between traces and logs in a backend (Jaeger ↔ Loki), but the four
// IDs here let an operator filter and correlate logs without an OTEL backend
// configured at all.  Both must land before the v0.2.3 logs endpoint
// (RFC 0018 Phase 4) is useful in the operator-facing CLI.
//
// Naming convention (RFC 0018 § D): all keys use the lowercase kebab-case
// `persatrix-` prefix.  The `x-` prefix is intentionally NOT used —
// RFC 6648 deprecates it for HTTP, and gRPC metadata follows the same
// convention.  The interceptor on the agent side strips the `persatrix-`
// prefix when binding to structlog contextvars so log records read
// `execution_id`, not `persatrix-execution-id`.
package grpcmeta

import (
	"context"

	"google.golang.org/grpc/metadata"
)

// Metadata key constants — the wire form sent on outbound gRPC.
const (
	// MDExecutionID is the workflow run identifier.  Maps to the
	// `execution_id` log field on both the Go and Python sides.
	MDExecutionID = "persatrix-execution-id"
	// MDStepID is the per-step identifier within an execution.  Maps to
	// `step_id` on logs.
	MDStepID = "persatrix-step-id"
	// MDAgentID is the destination agent identifier.  Maps to `agent_id`.
	MDAgentID = "persatrix-agent-id"
	// MDWorkflowID is the workflow definition identifier (independent of the
	// per-execution `MDExecutionID`).  Maps to `workflow_id`.
	MDWorkflowID = "persatrix-workflow-id"
)

// IDs carries the four correlation IDs propagated across the gRPC boundary.
// Empty fields are not injected — partial sets are valid (e.g. chat dispatch
// has no execution_id / step_id because chat lives outside the workflow
// scheduler).
type IDs struct {
	ExecutionID string
	StepID      string
	AgentID     string
	WorkflowID  string
}

// InjectIDs returns ctx with the non-empty IDs appended to the outgoing gRPC
// metadata.  Existing metadata on ctx is preserved (AppendToOutgoingContext
// merges); the four IDs replace any previously-injected values for the same
// keys (last-write-wins, matching gRPC metadata semantics).
//
// Callers should invoke this once per RPC immediately before the client
// call so that retries within the same dispatch pick up the injected
// metadata via the same ctx.
func InjectIDs(ctx context.Context, ids IDs) context.Context {
	pairs := make([]string, 0, 8)
	if ids.ExecutionID != "" {
		pairs = append(pairs, MDExecutionID, ids.ExecutionID)
	}
	if ids.StepID != "" {
		pairs = append(pairs, MDStepID, ids.StepID)
	}
	if ids.AgentID != "" {
		pairs = append(pairs, MDAgentID, ids.AgentID)
	}
	if ids.WorkflowID != "" {
		pairs = append(pairs, MDWorkflowID, ids.WorkflowID)
	}
	if len(pairs) == 0 {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, pairs...)
}

// ExtractIDs reads the four metadata keys from ctx's incoming gRPC metadata
// and returns whatever is present.  Missing or empty keys produce empty
// fields (no error — partial sets are valid per InjectIDs).  The function is
// the symmetric counterpart to InjectIDs and is exported so future Go-side
// gRPC servers (e.g. the orchestrator's LogService in RFC 0018 PR 5) can
// bind these IDs onto incoming requests without re-deriving the wire-key
// constants.
func ExtractIDs(ctx context.Context) IDs {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return IDs{}
	}
	return IDs{
		ExecutionID: firstOrEmpty(md.Get(MDExecutionID)),
		StepID:      firstOrEmpty(md.Get(MDStepID)),
		AgentID:     firstOrEmpty(md.Get(MDAgentID)),
		WorkflowID:  firstOrEmpty(md.Get(MDWorkflowID)),
	}
}

func firstOrEmpty(vals []string) string {
	if len(vals) == 0 {
		return ""
	}
	return vals[0]
}
