package grpcmeta

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/metadata"
)

func TestInjectIDs_AppendsAllKeys(t *testing.T) {
	ctx := InjectIDs(context.Background(), IDs{
		ExecutionID: "exec-1",
		StepID:      "step-A",
		AgentID:     "ember-owl",
		WorkflowID:  "wf-7",
	})

	md, ok := metadata.FromOutgoingContext(ctx)
	require.True(t, ok, "outgoing metadata should be present")
	require.Equal(t, []string{"exec-1"}, md.Get(MDExecutionID))
	require.Equal(t, []string{"step-A"}, md.Get(MDStepID))
	require.Equal(t, []string{"ember-owl"}, md.Get(MDAgentID))
	require.Equal(t, []string{"wf-7"}, md.Get(MDWorkflowID))
}

func TestInjectIDs_SkipsEmptyFields(t *testing.T) {
	ctx := InjectIDs(context.Background(), IDs{AgentID: "ember-owl"})

	md, ok := metadata.FromOutgoingContext(ctx)
	require.True(t, ok)
	require.Empty(t, md.Get(MDExecutionID))
	require.Empty(t, md.Get(MDStepID))
	require.Empty(t, md.Get(MDWorkflowID))
	require.Equal(t, []string{"ember-owl"}, md.Get(MDAgentID))
}

func TestInjectIDs_NoOpOnEmptyStruct(t *testing.T) {
	ctx := InjectIDs(context.Background(), IDs{})
	// No outgoing metadata appended at all — kept context unchanged.
	_, ok := metadata.FromOutgoingContext(ctx)
	require.False(t, ok, "empty IDs should not create outgoing metadata")
}

func TestInjectIDs_PreservesExistingMetadata(t *testing.T) {
	base := metadata.AppendToOutgoingContext(context.Background(), "existing-key", "existing-val")
	ctx := InjectIDs(base, IDs{AgentID: "ember-owl"})

	md, ok := metadata.FromOutgoingContext(ctx)
	require.True(t, ok)
	require.Equal(t, []string{"existing-val"}, md.Get("existing-key"))
	require.Equal(t, []string{"ember-owl"}, md.Get(MDAgentID))
}

func TestExtractIDs_RoundTripFromIncoming(t *testing.T) {
	// Simulate the agent-side context — gRPC moves outgoing → incoming
	// across the wire boundary.  We test the round-trip via the same
	// metadata.MD object.
	md := metadata.New(map[string]string{
		MDExecutionID: "exec-9",
		MDStepID:      "step-Z",
		MDAgentID:     "frost-fox",
		MDWorkflowID:  "wf-42",
	})
	ctx := metadata.NewIncomingContext(context.Background(), md)

	ids := ExtractIDs(ctx)
	require.Equal(t, IDs{
		ExecutionID: "exec-9",
		StepID:      "step-Z",
		AgentID:     "frost-fox",
		WorkflowID:  "wf-42",
	}, ids)
}

func TestExtractIDs_MissingKeysReturnEmptyFields(t *testing.T) {
	md := metadata.New(map[string]string{MDAgentID: "ember-owl"})
	ctx := metadata.NewIncomingContext(context.Background(), md)

	ids := ExtractIDs(ctx)
	require.Equal(t, "ember-owl", ids.AgentID)
	require.Empty(t, ids.ExecutionID)
	require.Empty(t, ids.StepID)
	require.Empty(t, ids.WorkflowID)
}

func TestExtractIDs_NoIncomingMetadataReturnsZero(t *testing.T) {
	ids := ExtractIDs(context.Background())
	require.Equal(t, IDs{}, ids)
}

func TestMetadataKeysAreLowercaseKebab(t *testing.T) {
	// gRPC normalises metadata keys to lowercase; assert the constants
	// match the wire form so a future refactor (e.g. accidental switch to
	// snake_case or X- prefix) fails this test rather than silently
	// breaking cross-process correlation.
	require.Equal(t, "persatrix-execution-id", MDExecutionID)
	require.Equal(t, "persatrix-step-id", MDStepID)
	require.Equal(t, "persatrix-agent-id", MDAgentID)
	require.Equal(t, "persatrix-workflow-id", MDWorkflowID)
}
