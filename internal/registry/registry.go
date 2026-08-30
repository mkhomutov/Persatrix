// Package registry manages agent registration, discovery, and health tracking.
package registry

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"

	"go.uber.org/zap"
)

// Sentinel errors for registry operations.
//
// ErrAgentAlreadyRegistered was removed with ISSUE-0125: Register is an upsert,
// so there is no duplicate to report. See the Register doc for why.
var (
	ErrAgentNotFound = errors.New("agent not found")
)

// AgentInfo holds metadata about a registered agent.
type AgentInfo struct {
	ID   string
	Name string
	Role string
	// Type is the agent kind from config/agents.yaml (`task` | `persona` | …),
	// "" when the registrant predates the field. It extends the RFC 0048
	// amendment §A agent DTO and is display/affordance metadata only — routing
	// and chat dispatch never read it. The web console uses it to disable chat
	// for task agents, which execute workflow steps and do not hold a conversation.
	Type         string
	Capabilities []string
	Address      string // gRPC address (host:port)
	NodeID       string // empty for local deployment
	Status       AgentStatus
	Model        string // LLM model identifier for cost estimation (e.g., "claude-sonnet")

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
	// NamesFor returns id→display-name for just the requested ids, omitting any
	// id with no registered row. It is the membership-scoped read the channel
	// mention lift needs (ISSUE-0100): one pass over a known id set instead of a
	// whole-directory List+sort when only a handful of names are wanted. Backings
	// can satisfy it in one scoped query (in-memory: N map reads under one lock;
	// a remote store: one `id IN (…)`), so it stays O(ids), never O(directory).
	NamesFor(ctx context.Context, ids []string) (map[string]string, error)
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

// Register adds an agent to the registry, REPLACING any row already held under
// the same ID. It is an upsert: it never fails on a duplicate, and the last
// registration wins.
//
// It used to reject duplicates with ErrAgentAlreadyRegistered — "re-registration
// requires calling Unregister first" (RFC 0001 Phase 2) — which the REST layer
// surfaced as 409 CONFLICT. ISSUE-0125 hoists that away because every shape of
// fleet re-registration inherits it as a precondition: against a POPULATED
// registry a re-register was a no-op, so an agent that moved to a new address
// could never correct it, and any boot-time seed would 409-block the agent's own
// registration — the one call that carries the real advertise address.
//
// The upsert is NOT itself the restart fix, and must not be read as one: after
// an orchestrator restart the map is empty and a re-register already succeeded
// without a 409. It covers the connection blip and the stale address; the agent
// side has to actually call again, which is what ReregistrationWatcher
// (agents/server_reregister.py) does.
//
// Agent ID format validation (^[a-z0-9][a-z0-9-]*[a-z0-9]$) is enforced at the
// REST API layer (RFC 0002), not in the registry. The registry accepts any non-empty
// string ID to avoid coupling to a specific format convention.
func (r *InMemoryRegistry) Register(_ context.Context, agent AgentInfo) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	_, replaced := r.agents[agent.ID]

	// Store an internal copy with Capabilities slice deep-copied.
	stored := &AgentInfo{
		ID:             agent.ID,
		Name:           agent.Name,
		Role:           agent.Role,
		Type:           agent.Type,
		Address:        agent.Address,
		NodeID:         agent.NodeID,
		Status:         agent.Status,
		Model:          agent.Model,
		MaxLLMCalls:    agent.MaxLLMCalls,
		MaxTokens:      agent.MaxTokens,
		TimeoutSeconds: agent.TimeoutSeconds,
	}
	stored.Capabilities = make([]string, len(agent.Capabilities))
	copy(stored.Capabilities, agent.Capabilities)

	r.agents[agent.ID] = stored
	r.logger.Debug("agent registered",
		zap.String("agent_id", agent.ID),
		zap.String("address", agent.Address),
		zap.Bool("replaced", replaced),
	)
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
	r.logger.Debug("agent unregistered", zap.String("agent_id", agentID))
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
	// Sort by ID so List returns a deterministic order. The agents map has a
	// randomized Go iteration order, so without this the sequence differed on
	// every call — the web console re-fetches the persona list on each tab switch
	// (RFC 0048), which reshuffled the dropdown each time. A stable order serves
	// every consumer (web picker, channel decoration, CLI) from one place.
	sort.Slice(result, func(i, j int) bool {
		return result[i].ID < result[j].ID
	})
	return result, nil
}

// NamesFor returns id→display-name for the requested ids, reading each under a
// single read lock. Ids with no registered row are omitted (not an error), so
// the caller treats a missing name as "id-only". Only the name string is copied
// — no AgentInfo deep-copy or directory sort, the cost the whole-directory List
// pays — keeping the call O(len(ids)) instead of O(registry). The returned map
// is always non-nil (empty for empty input), matching List/FindByCapability.
func (r *InMemoryRegistry) NamesFor(_ context.Context, ids []string) (map[string]string, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	names := make(map[string]string, len(ids))
	for _, id := range ids {
		if agent, exists := r.agents[id]; exists {
			names[id] = agent.Name
		}
	}
	return names, nil
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
	r.logger.Debug("agent status updated", zap.String("agent_id", agentID), zap.Int("status", int(status)))
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
		Type:           agent.Type,
		Address:        agent.Address,
		NodeID:         agent.NodeID,
		Status:         agent.Status,
		Model:          agent.Model,
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
