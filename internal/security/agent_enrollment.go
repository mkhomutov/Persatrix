// Agent enrollment (RFC 0009 Phase 4; Phases 3–4 plan PR 6). The blank line
// below keeps this out of the package comment, which is in security.go.

package security

import (
	"context"
	"errors"
	"sync"

	"github.com/mkhomutov/persatrix/internal/registry"
)

// AgentEnrollment registers an agent and issues its token as one step, and
// deregisters an agent and revokes its tokens as one step, so the registry
// and the token issuer never disagree about an agent (PR 5 review F-6). An
// orchestrator makes one and hands it to every server that registers
// agents, so no second way in can fall between the two. It also remembers
// where each agent last registered from, so a registration from somewhere
// else can be audited (G-14). It is safe for concurrent use.
type AgentEnrollment struct {
	registry registry.Registry
	tokens   *AgentTokenIssuer

	mu sync.Mutex
	// sources is where each registered agent's latest registration came
	// from, by agent ID.
	sources map[string]string
}

// AgentEnrolled is what a registration did.
type AgentEnrolled struct {
	Token  string
	Claims AgentTokenClaims
	// Previous is the agent as it was registered before, nil for a first
	// registration.
	Previous *registry.AgentInfo
	// PreviousSource is where the earlier registration came from, "" when
	// there was none or it is not known.
	PreviousSource string
}

// NewAgentEnrollment makes an enrollment over reg, the registry the servers
// read agents from, and tokens, the issuer that signs and checks their
// tokens.
func NewAgentEnrollment(reg registry.Registry, tokens *AgentTokenIssuer) *AgentEnrollment {
	return &AgentEnrollment{registry: reg, tokens: tokens, sources: make(map[string]string)}
}

// Issuer is the token issuer the enrollment signs tokens with.
func (e *AgentEnrollment) Issuer() *AgentTokenIssuer { return e.tokens }

// Enroll registers info, which came from source, and issues its agent a
// token, which retires the agent's earlier ones (PR 5 review F-12). When no
// token can be issued, the registry is put back as it was, so no agent is
// left registered without one.
func (e *AgentEnrollment) Enroll(ctx context.Context, info registry.AgentInfo, source string) (AgentEnrolled, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	previous, err := e.registry.Get(ctx, info.ID)
	if errors.Is(err, registry.ErrAgentNotFound) {
		previous, err = nil, nil
	}
	if err != nil {
		return AgentEnrolled{}, err
	}
	if err = e.registry.Register(ctx, info); err != nil {
		return AgentEnrolled{}, err
	}
	token, claims, err := e.tokens.Issue(info.ID)
	if err != nil {
		return AgentEnrolled{}, errors.Join(err, e.undo(ctx, info.ID, previous))
	}
	enrolled := AgentEnrolled{Token: token, Claims: claims, Previous: previous}
	if previous != nil {
		enrolled.PreviousSource = e.sources[info.ID]
	}
	e.sources[info.ID] = source
	return enrolled, nil
}

// undo puts the registry back as it was before a registration of id.
func (e *AgentEnrollment) undo(ctx context.Context, id string, previous *registry.AgentInfo) error {
	if previous != nil {
		return e.registry.Register(ctx, *previous)
	}
	return e.registry.Unregister(ctx, id)
}

// Deregister removes the agent and revokes its tokens. An agent the
// registry does not hold gets [registry.ErrAgentNotFound], and nothing is
// revoked.
func (e *AgentEnrollment) Deregister(ctx context.Context, id string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := e.registry.Unregister(ctx, id); err != nil {
		return err
	}
	e.tokens.Revoke(id)
	delete(e.sources, id)
	return nil
}
