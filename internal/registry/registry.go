// Package registry manages agent registration, discovery, and health tracking.
package registry

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"go.uber.org/zap"
)

// Sentinel errors for registry operations.
var (
	ErrAgentAlreadyRegistered = errors.New("agent already registered")
	ErrAgentNotFound          = errors.New("agent not found")
)

// AgentInfo holds metadata about a registered agent.
type AgentInfo struct {
	ID           string
	Name         string
	Role         string
	Capabilities []string
	Address      string // gRPC address (host:port)
	NodeID       string // empty for local deployment
	Status       AgentStatus

	// Execution limit fields (RFC 0006). Zero means "not configured at agent
	// level" — the scheduler falls through to system defaults.
	MaxLLMCalls    int
	MaxTokens      int
	TimeoutSeconds int
}

type AgentStatus int

const (
	StatusUnknown AgentStatus = iota
	StatusHealthy
	StatusDegraded
	StatusOffline
)

// Registry manages the lifecycle of registered agents.
type Registry interface {
	Register(ctx context.Context, agent AgentInfo) error
	Unregister(ctx context.Context, agentID string) error
	Get(ctx context.Context, agentID string) (*AgentInfo, error)
	List(ctx context.Context) ([]AgentInfo, error)
	UpdateStatus(ctx context.Context, agentID string, status AgentStatus) error
	FindByCapability(ctx context.Context, capability string) ([]AgentInfo, error)
}

// InMemoryRegistry is a goroutine-safe in-memory implementation of Registry.
type InMemoryRegistry struct {
	mu     sync.RWMutex
	agents map[string]*AgentInfo
	logger *zap.Logger
}

// NewInMemoryRegistry creates a new in-memory agent registry.
func NewInMemoryRegistry(logger *zap.Logger) *InMemoryRegistry {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &InMemoryRegistry{
		agents: make(map[string]*AgentInfo),
		logger: logger,
	}
}

// Register adds a new agent to the registry. Returns ErrAgentAlreadyRegistered
// if an agent with the same ID is already registered. Re-registration requires
// calling Unregister first (non-atomic; see RFC 0001 Phase 2 notes).
//
// Agent ID format validation (^[a-z0-9][a-z0-9-]*[a-z0-9]$) is enforced at the
// REST API layer (RFC 0002), not in the registry. The registry accepts any non-empty
// string ID to avoid coupling to a specific format convention.
func (r *InMemoryRegistry) Register(_ context.Context, agent AgentInfo) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	if _, exists := r.agents[agent.ID]; exists {
		return ErrAgentAlreadyRegistered
	}

	// Store an internal copy with Capabilities slice deep-copied.
	stored := &AgentInfo{
		ID:             agent.ID,
		Name:           agent.Name,
		Role:           agent.Role,
		Address:        agent.Address,
		NodeID:         agent.NodeID,
		Status:         agent.Status,
		MaxLLMCalls:    agent.MaxLLMCalls,
		MaxTokens:      agent.MaxTokens,
		TimeoutSeconds: agent.TimeoutSeconds,
	}
	stored.Capabilities = make([]string, len(agent.Capabilities))
	copy(stored.Capabilities, agent.Capabilities)

	r.agents[agent.ID] = stored
	r.logger.Debug("agent registered", zap.String("agentID", agent.ID), zap.String("address", agent.Address))
	return nil
}

// Unregister removes an agent from the registry.
// Returns ErrAgentNotFound if the agent ID does not exist.
func (r *InMemoryRegistry) Unregister(_ context.Context, agentID string) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	if _, exists := r.agents[agentID]; !exists {
		return ErrAgentNotFound
	}

	delete(r.agents, agentID)
	r.logger.Debug("agent unregistered", zap.String("agentID", agentID))
	return nil
}

// Get retrieves an agent by ID. Returns a deep copy to prevent callers from
// mutating internal state. Returns ErrAgentNotFound on miss.
func (r *InMemoryRegistry) Get(_ context.Context, agentID string) (*AgentInfo, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	agent, exists := r.agents[agentID]
	if !exists {
		return nil, ErrAgentNotFound
	}

	return deepCopyAgent(agent), nil
}

// List returns deep copies of all registered agents.
func (r *InMemoryRegistry) List(_ context.Context) ([]AgentInfo, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	result := make([]AgentInfo, 0, len(r.agents))
	for _, agent := range r.agents {
		result = append(result, *deepCopyAgent(agent))
	}
	return result, nil
}

// UpdateStatus updates the status of a registered agent.
// Returns ErrAgentNotFound if the agent ID does not exist.
func (r *InMemoryRegistry) UpdateStatus(_ context.Context, agentID string, status AgentStatus) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	agent, exists := r.agents[agentID]
	if !exists {
		return ErrAgentNotFound
	}

	agent.Status = status
	r.logger.Debug("agent status updated", zap.String("agentID", agentID), zap.Int("status", int(status)))
	return nil
}

// FindByCapability returns deep copies of all agents that have the specified capability.
// Returns an empty non-nil slice (not nil) when no agents match, ensuring consistent
// JSON serialization as [] rather than null (PR #12 review F-07, consistent with List).
func (r *InMemoryRegistry) FindByCapability(_ context.Context, capability string) ([]AgentInfo, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	result := make([]AgentInfo, 0)
	for _, agent := range r.agents {
		for _, capName := range agent.Capabilities {
			if capName == capability {
				result = append(result, *deepCopyAgent(agent))
				break
			}
		}
	}
	return result, nil
}

// deepCopyAgent creates a deep copy of an AgentInfo, reconstructing the
// Capabilities slice to prevent shared backing-array mutation.
func deepCopyAgent(agent *AgentInfo) *AgentInfo {
	cp := &AgentInfo{
		ID:             agent.ID,
		Name:           agent.Name,
		Role:           agent.Role,
		Address:        agent.Address,
		NodeID:         agent.NodeID,
		Status:         agent.Status,
		MaxLLMCalls:    agent.MaxLLMCalls,
		MaxTokens:      agent.MaxTokens,
		TimeoutSeconds: agent.TimeoutSeconds,
	}
	cp.Capabilities = make([]string, len(agent.Capabilities))
	copy(cp.Capabilities, agent.Capabilities)
	return cp
}

// String returns a human-readable representation of AgentStatus for logging.
func (s AgentStatus) String() string {
	switch s {
	case StatusUnknown:
		return "Unknown"
	case StatusHealthy:
		return "Healthy"
	case StatusDegraded:
		return "Degraded"
	case StatusOffline:
		return "Offline"
	default:
		return fmt.Sprintf("AgentStatus(%d)", int(s))
	}
}

// TODO: Implement health check loop
