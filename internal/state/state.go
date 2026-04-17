// Package state manages execution state, checkpoints, and persistence.
package state

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"
)

// Sentinel errors for state store operations.
var (
	ErrRunAlreadyExists = errors.New("run already exists")
	ErrRunNotFound      = errors.New("run not found")
)

// RunStatus represents the execution status of a workflow run or step.
// Values are explicit integers aligned with proto/task.proto TaskStatus (0–4).
// Do NOT use iota — inserting intermediate values silently renumbers constants.
type RunStatus int

const (
	RunPending   RunStatus = 0
	RunRunning   RunStatus = 1
	RunCompleted RunStatus = 2
	RunFailed    RunStatus = 3
	RunCancelled RunStatus = 4
	RunRetrying  RunStatus = 5
)

// WorkflowRun tracks the state of a single workflow execution.
type WorkflowRun struct {
	ID         string
	WorkflowID string
	Status     RunStatus
	Steps      map[string]StepState
	Error      string
	StartedAt  time.Time
	FinishedAt time.Time
	Inputs     map[string]string
}

// StepExecutionMetadata captures observability data for a completed step.
// This is a Go struct (not a proto message per RFC 0006 Section A) — stored
// in StepState and serialized to JSON in API responses.
type StepExecutionMetadata struct {
	TokensUsed       int     `json:"tokens_used"`
	LLMCallCount     int     `json:"llm_call_count"`
	RetryCount       int     `json:"retry_count"`
	CacheHit         bool    `json:"cache_hit"`
	WallTimeMs       int64   `json:"wall_time_ms"`
	EstimatedCostUSD float64 `json:"estimated_cost_usd"`
}

// StepState tracks the state of a single step within a workflow run.
type StepState struct {
	StepID     string
	Status     RunStatus
	Output     string
	Error      string
	StartedAt  time.Time
	FinishedAt time.Time
	Metadata   *StepExecutionMetadata
}

// Store defines the interface for workflow run state persistence.
// All methods accept context.Context for forward compatibility with
// persistent backends (SQLite in v0.2). In-memory implementations
// may ignore the context parameter.
type Store interface {
	CreateRun(ctx context.Context, run *WorkflowRun) error
	GetRun(ctx context.Context, runID string) (*WorkflowRun, error)
	ListRuns(ctx context.Context) ([]*WorkflowRun, error)
	UpdateRunStatus(ctx context.Context, runID string, status RunStatus) error
	UpdateStepState(ctx context.Context, runID string, step StepState) error
	DeleteRun(ctx context.Context, runID string) error
	SetRunTimestamps(ctx context.Context, runID string, startedAt, finishedAt *time.Time) error
	SetRunError(ctx context.Context, runID string, errMsg string) error
}

// InMemoryStore is a goroutine-safe in-memory implementation of Store.
// All state is lost on process restart.
type InMemoryStore struct {
	mu     sync.RWMutex
	runs   map[string]*WorkflowRun
	logger *zap.Logger
}

// NewInMemoryStore creates a new in-memory state store.
func NewInMemoryStore(logger *zap.Logger) *InMemoryStore {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &InMemoryStore{
		runs:   make(map[string]*WorkflowRun),
		logger: logger,
	}
}

// CreateRun adds a new workflow run to the store. If run.ID is empty, a UUIDv4
// is generated. Returns ErrRunAlreadyExists if a run with the same ID exists.
func (s *InMemoryStore) CreateRun(_ context.Context, run *WorkflowRun) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if run.ID == "" {
		run.ID = uuid.New().String()
	}

	if _, exists := s.runs[run.ID]; exists {
		return ErrRunAlreadyExists
	}

	// Deep copy to prevent caller from mutating internal state.
	stored := &WorkflowRun{
		ID:         run.ID,
		WorkflowID: run.WorkflowID,
		Status:     run.Status,
		Error:      run.Error,
		StartedAt:  run.StartedAt,
		FinishedAt: run.FinishedAt,
	}

	// Initialize Steps map (prevent nil-map panics on subsequent writes).
	stored.Steps = make(map[string]StepState, len(run.Steps))
	for k, v := range run.Steps {
		stored.Steps[k] = v
	}

	stored.Inputs = make(map[string]string, len(run.Inputs))
	for k, v := range run.Inputs {
		stored.Inputs[k] = v
	}

	s.runs[run.ID] = stored
	s.logger.Debug("run created", zap.String("runID", run.ID), zap.String("workflowID", run.WorkflowID))
	return nil
}

// GetRun retrieves a workflow run by ID. Returns a deep copy to prevent
// callers from mutating internal state. Returns ErrRunNotFound on miss.
func (s *InMemoryStore) GetRun(_ context.Context, runID string) (*WorkflowRun, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	run, exists := s.runs[runID]
	if !exists {
		return nil, ErrRunNotFound
	}

	return deepCopyRun(run), nil
}

// ListRuns returns deep copies of all workflow runs.
func (s *InMemoryStore) ListRuns(_ context.Context) ([]*WorkflowRun, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	result := make([]*WorkflowRun, 0, len(s.runs))
	for _, run := range s.runs {
		result = append(result, deepCopyRun(run))
	}
	return result, nil
}

// UpdateRunStatus updates the status of a workflow run.
// Returns ErrRunNotFound if the run does not exist.
// No state transition validation in v0.1 — any transition is allowed.
func (s *InMemoryStore) UpdateRunStatus(_ context.Context, runID string, status RunStatus) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	run, exists := s.runs[runID]
	if !exists {
		return ErrRunNotFound
	}

	run.Status = status
	s.logger.Debug("run status updated", zap.String("runID", runID), zap.Int("status", int(status)))
	return nil
}

// UpdateStepState merges step state into the run's Steps map.
// If the step ID is not already present, it is added (supports Scheduler
// initializing step state on first execution without pre-population).
// Returns ErrRunNotFound if the run does not exist.
func (s *InMemoryStore) UpdateStepState(_ context.Context, runID string, step StepState) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	run, exists := s.runs[runID]
	if !exists {
		return ErrRunNotFound
	}

	// Deep copy metadata pointer to prevent caller from mutating store-internal
	// state via a shared pointer. Matches CreateRun and deepCopyRun deep-copy
	// behavior — all write paths now consistently own their data. (PR 5a, M-02)
	if step.Metadata != nil {
		metaCopy := *step.Metadata
		step.Metadata = &metaCopy
	}

	run.Steps[step.StepID] = step
	s.logger.Debug("step state updated", zap.String("runID", runID), zap.String("stepID", step.StepID))
	return nil
}

// DeleteRun removes a workflow run from the store.
// Accepts any run status in v0.1 (status restriction deferred to RFC 0003).
// Returns ErrRunNotFound if the run does not exist.
func (s *InMemoryStore) DeleteRun(_ context.Context, runID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, exists := s.runs[runID]; !exists {
		return ErrRunNotFound
	}

	delete(s.runs, runID)
	s.logger.Debug("run deleted", zap.String("runID", runID))
	return nil
}

// SetRunTimestamps updates the StartedAt and/or FinishedAt timestamps on a
// workflow run. A nil pointer means "leave unchanged". Deep-copy semantics
// are maintained for timestamp fields.
// Returns ErrRunNotFound if the run does not exist.
func (s *InMemoryStore) SetRunTimestamps(_ context.Context, runID string, startedAt, finishedAt *time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	run, exists := s.runs[runID]
	if !exists {
		return ErrRunNotFound
	}

	if startedAt != nil {
		run.StartedAt = *startedAt
	}
	if finishedAt != nil {
		run.FinishedAt = *finishedAt
	}

	s.logger.Debug("run timestamps updated", zap.String("runID", runID))
	return nil
}

// SetRunError sets the Error field on a workflow run. Used by the scheduler's
// failRun helper to persist failure reasons.
// Returns ErrRunNotFound if the run does not exist.
func (s *InMemoryStore) SetRunError(_ context.Context, runID string, errMsg string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	run, exists := s.runs[runID]
	if !exists {
		return ErrRunNotFound
	}

	run.Error = errMsg
	s.logger.Debug("run error set", zap.String("runID", runID), zap.String("error", errMsg))
	return nil
}

// deepCopyRun creates a deep copy of a WorkflowRun, reconstructing both the
// Steps and Inputs maps to prevent shared backing-reference mutation.
func deepCopyRun(run *WorkflowRun) *WorkflowRun {
	cp := &WorkflowRun{
		ID:         run.ID,
		WorkflowID: run.WorkflowID,
		Status:     run.Status,
		Error:      run.Error,
		StartedAt:  run.StartedAt,
		FinishedAt: run.FinishedAt,
	}

	cp.Steps = make(map[string]StepState, len(run.Steps))
	for k, v := range run.Steps {
		if v.Metadata != nil {
			metaCopy := *v.Metadata
			v.Metadata = &metaCopy
		}
		cp.Steps[k] = v
	}

	cp.Inputs = make(map[string]string, len(run.Inputs))
	for k, v := range run.Inputs {
		cp.Inputs[k] = v
	}

	return cp
}

// String returns a human-readable representation of RunStatus for logging.
func (s RunStatus) String() string {
	switch s {
	case RunPending:
		return "Pending"
	case RunRunning:
		return "Running"
	case RunCompleted:
		return "Completed"
	case RunFailed:
		return "Failed"
	case RunCancelled:
		return "Cancelled"
	case RunRetrying:
		return "Retrying"
	default:
		return fmt.Sprintf("RunStatus(%d)", int(s))
	}
}

// TODO: Implement SQLiteStore (v0.2+)
// TODO: Implement checkpoint/restore
// TODO: Implement export/import
