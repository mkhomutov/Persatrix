// Package registry manages agent registration, discovery, and health tracking.
package registry

import "context"

// AgentInfo holds metadata about a registered agent.
type AgentInfo struct {
	ID           string
	Name         string
	Role         string
	Capabilities []string
	Address      string      // gRPC address (host:port)
	NodeID       string      // empty for local deployment
	Status       AgentStatus
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

// TODO: Implement InMemoryRegistry
// TODO: Implement health check loop
