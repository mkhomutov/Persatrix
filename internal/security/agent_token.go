// Agent tokens (RFC 0009 §B; Phases 3–4 plan PR 5, design point G-5, open
// question 4). The blank line below keeps this out of the package comment,
// which is in security.go.

package security

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"math"
	"strings"
	"sync"
	"time"
)

// AgentTokenTTL is how long an agent token works after it is issued.
const AgentTokenTTL = 24 * time.Hour

// The token format, described on [AgentTokenIssuer].
const (
	// agentTokenPrefix names the format, so a later format can be told
	// apart, and makes a token easy to spot in text such as a pasted log.
	agentTokenPrefix = "agt1."

	agentTokenKeySize   = 32 // an HMAC-SHA256 key
	agentTokenNonceSize = 16

	// agentTokenFixedSize is the fields before the agent ID: the issue time
	// and the expiry, each 8 bytes of Unix nanoseconds, then the nonce.
	agentTokenFixedSize = 8 + 8 + agentTokenNonceSize
)

// agentTokenSweepFloor is how many agents the issuer holds before Issue
// first forgets the ones whose tokens have all expired.
const agentTokenSweepFloor = 64

// maxAgentTokenLen is the longest token Issue makes, the one for an ID of
// [MaxAgentIDLen]. Check refuses anything longer before doing any work.
var maxAgentTokenLen = len(agentTokenPrefix) +
	base64.RawURLEncoding.EncodedLen(agentTokenFixedSize+MaxAgentIDLen) + 1 +
	base64.RawURLEncoding.EncodedLen(sha256.Size)

// Why a token is refused. They stay apart for the audit line; what a caller
// is told is the server's choice.
var (
	// ErrAgentTokenInvalid: not a token this process issued. It is garbled,
	// changed, or signed with another key, such as the one before a restart.
	ErrAgentTokenInvalid = errors.New("security: agent token is not valid")
	// ErrAgentTokenExpired: the token is past its [AgentTokenTTL].
	ErrAgentTokenExpired = errors.New("security: agent token has expired")
	// ErrAgentTokenRevoked: the agent's tokens were revoked after this one
	// was issued, as when the agent deregistered.
	ErrAgentTokenRevoked = errors.New("security: agent token has been revoked")
	// ErrAgentTokenAgentID: Issue was asked for an ID no agent can have:
	// empty, off the agent ID pattern, or over 256 bytes.
	ErrAgentTokenAgentID = errors.New("security: an agent token's agent ID must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$ and be at most 256 bytes")
)

// AgentTokenClaims is what a token says: which agent it was issued to, and
// when it was issued and expires.
type AgentTokenClaims struct {
	AgentID   string
	IssuedAt  time.Time
	ExpiresAt time.Time
}

// AgentTokenConfig configures [NewAgentTokenIssuer].
type AgentTokenConfig struct {
	// Now is the clock; nil means [time.Now]. Tests set their own.
	Now func() time.Time
}

// AgentTokenIssuer issues agent tokens, checks them and revokes them. An
// agent token proves which agent is calling: the orchestrator gives one to an
// agent when it registers, so on its later calls the agent shows the token
// instead of only naming itself, which anyone can do. An orchestrator makes
// one issuer when it starts. It is safe for concurrent use.
//
// A token reads
//
//	agt1.<fields>.<signature>
//
// The fields are the agent's ID, when the token was issued, when it expires
// and a random nonce. The signature is HMAC-SHA256 over everything before it,
// under a key the issuer makes from crypto/rand and never stores or hands
// out. So nobody without the key can make a token or change one, and a
// restart stops every earlier token from working. Both parts are base64url
// without padding, so a token fits an HTTP header or gRPC metadata as it is.
// A token lists nothing the agent may do: nothing in v0.4.0 would read such
// a list.
//
// The issuer keeps its own time: the time of day when it was made, plus the
// time that has passed since (see [AgentTokenIssuer.clock]). So a token
// works for 24 hours however a time sync moves the computer's clock, and its
// IssuedAt and ExpiresAt are dates counted from when the issuer was made.
//
// Revocations are kept in this process only, since v0.4.0 runs one
// orchestrator: [AgentTokenIssuer.Revoke] stops every token one agent holds,
// and a restart stops all of them by making a new key.
type AgentTokenIssuer struct {
	// key signs every token. It sits behind a pointer, so printing the
	// issuer, or a value that holds one, shows an address rather than the
	// key; no method returns it.
	key   *[agentTokenKeySize]byte
	now   func() time.Time
	start time.Time

	mu sync.RWMutex
	// last is the latest moment Issue or Revoke recorded, in Unix
	// nanoseconds (see [AgentTokenIssuer.moment]).
	last int64
	// agents holds what the issuer knows of each agent while a token issued
	// to it may still work. Issue forgets the others once the map has
	// reached sweepAt.
	agents  map[string]agentTokenState
	sweepAt int
}

// agentTokenState is what the issuer keeps about one agent.
type agentTokenState struct {
	lastExpiry int64 // when its latest token expires, in Unix nanoseconds
	revokedAt  int64 // the moment of its latest revocation; math.MinInt64 if none
}

// NewAgentTokenIssuer makes an issuer with a new key from crypto/rand.
func NewAgentTokenIssuer(cfg AgentTokenConfig) *AgentTokenIssuer {
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	key := new([agentTokenKeySize]byte)
	// rand.Read never returns an error: if the system's random source
	// fails, it stops the program instead.
	_, _ = rand.Read(key[:])
	return &AgentTokenIssuer{
		key:     key,
		now:     now,
		start:   now(),
		last:    math.MinInt64,
		agents:  make(map[string]agentTokenState),
		sweepAt: agentTokenSweepFloor,
	}
}

// Issue makes a token for agentID that works for [AgentTokenTTL], and says
// what the token holds. An ID no agent can have gets [ErrAgentTokenAgentID].
func (t *AgentTokenIssuer) Issue(agentID string) (string, AgentTokenClaims, error) {
	if agentID == "" || !validAgentID(agentID) {
		return "", AgentTokenClaims{}, ErrAgentTokenAgentID
	}
	p := agentTokenPayload{agentID: agentID}
	_, _ = rand.Read(p.nonce[:]) // never fails; see NewAgentTokenIssuer

	t.mu.Lock()
	p.issued = t.moment()
	p.expires = p.issued + int64(AgentTokenTTL)
	if len(t.agents) >= t.sweepAt {
		t.forgetExpired(p.issued)
	}
	state, known := t.agents[agentID]
	if !known {
		state.revokedAt = math.MinInt64
	}
	state.lastExpiry = p.expires
	t.agents[agentID] = state
	t.mu.Unlock()

	signed := agentTokenPrefix + base64.RawURLEncoding.EncodeToString(p.marshal())
	return signed + "." + t.sign(signed), p.claims(), nil
}

// Check says which agent a token was issued to, or why the token is refused:
// [ErrAgentTokenInvalid], [ErrAgentTokenExpired] or [ErrAgentTokenRevoked].
// The signature is checked first, against the token's text exactly as sent,
// and nothing else in the token is read unless it matches.
func (t *AgentTokenIssuer) Check(token string) (AgentTokenClaims, error) {
	if len(token) > maxAgentTokenLen {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}
	dot := strings.LastIndexByte(token, '.')
	if dot < len(agentTokenPrefix) || !strings.HasPrefix(token, agentTokenPrefix) {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}
	signed, signature := token[:dot], token[dot+1:]
	// hmac.Equal takes the same time wherever two signatures differ, so the
	// time a refusal takes cannot help anyone guess a signature.
	if !hmac.Equal([]byte(signature), []byte(t.sign(signed))) {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(signed[len(agentTokenPrefix):])
	if err != nil {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}
	p, ok := unmarshalAgentTokenPayload(raw)
	if !ok {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}

	// The time and the agent's state are read together, so nothing can
	// forget a revocation between the two.
	t.mu.RLock()
	now := t.current()
	state, known := t.agents[p.agentID]
	t.mu.RUnlock()
	switch {
	case now >= p.expires:
		return AgentTokenClaims{}, ErrAgentTokenExpired
	case !known:
		// The issuer forgets an agent only once all its tokens have
		// expired, so an unexpired token for an agent it does not know
		// was not issued here.
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	case p.issued <= state.revokedAt:
		return AgentTokenClaims{}, ErrAgentTokenRevoked
	}
	return p.claims(), nil
}

// Revoke stops every token issued to agentID so far, as deregistering the
// agent needs. A token issued to it afterwards works, so the agent can
// register again. An agent holding no token that still works has nothing to
// revoke, so then the call changes nothing and keeps nothing.
func (t *AgentTokenIssuer) Revoke(agentID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	state, known := t.agents[agentID]
	if !known {
		return
	}
	state.revokedAt = t.moment()
	t.agents[agentID] = state
}

// clock returns the issuer's time in Unix nanoseconds: the time of day when
// it was made, plus the time passed since. The time passed comes from the
// clock's monotonic reading, which a time sync does not move, so no step of
// the computer's clock can bring back a revoked token or stretch a token's
// life; time a suspended machine spends asleep may not count. time.Now gives
// that reading; a test clock has none, so its readings count as they are.
func (t *AgentTokenIssuer) clock() int64 {
	return t.start.UnixNano() + int64(t.now().Sub(t.start))
}

// current is the issuer's time for a decision: the clock, but never earlier
// than a moment already recorded. forgetExpired runs at a recorded moment,
// so even a clock that went back cannot make a token it forgot about look
// unexpired. The caller holds t.mu.
func (t *AgentTokenIssuer) current() int64 {
	return max(t.clock(), t.last)
}

// moment returns the issuer's time, or a nanosecond past the last moment it
// returned if the time has not moved past that one. So each moment is later
// than the one before, even when the clock stands still, and a token issued
// after a revocation always has a later moment than the revocation, which
// therefore never catches it. The caller holds t.mu for writing.
func (t *AgentTokenIssuer) moment() int64 {
	t.last = max(t.clock(), t.last+1)
	return t.last
}

// forgetExpired drops each agent whose tokens have all expired by now: none
// can pass Check, so its revocation guards nothing. Issue calls it only once
// the map has doubled since the last time, so its cost, spread over the
// Issues that grew the map, stays the same however many agents there are,
// and the map stays within about twice the agents whose tokens still work.
// The caller holds t.mu for writing, and now is a moment it recorded.
func (t *AgentTokenIssuer) forgetExpired(now int64) {
	for id, state := range t.agents {
		if now >= state.lastExpiry {
			delete(t.agents, id)
		}
	}
	t.sweepAt = max(agentTokenSweepFloor, 2*len(t.agents))
}

// sign returns the signature for signed, the token's text before its last
// dot: HMAC-SHA256 under the issuer's key, in base64url.
func (t *AgentTokenIssuer) sign(signed string) string {
	mac := hmac.New(sha256.New, t.key[:])
	mac.Write([]byte(signed))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// agentTokenPayload is a token's fields, which its signature covers.
type agentTokenPayload struct {
	agentID string
	issued  int64 // Unix nanoseconds
	expires int64 // Unix nanoseconds
	nonce   [agentTokenNonceSize]byte
}

// marshal lays the fields out as a token carries them: the issue time and
// the expiry as 8-byte big-endian integers, the nonce, then the agent ID.
func (p agentTokenPayload) marshal() []byte {
	b := make([]byte, 0, agentTokenFixedSize+len(p.agentID))
	b = binary.BigEndian.AppendUint64(b, uint64(p.issued))
	b = binary.BigEndian.AppendUint64(b, uint64(p.expires))
	b = append(b, p.nonce[:]...)
	return append(b, p.agentID...)
}

// unmarshalAgentTokenPayload reads what marshal wrote, and reports false
// for bytes too short to hold an agent ID.
func unmarshalAgentTokenPayload(b []byte) (agentTokenPayload, bool) {
	if len(b) <= agentTokenFixedSize {
		return agentTokenPayload{}, false
	}
	p := agentTokenPayload{
		agentID: string(b[agentTokenFixedSize:]),
		issued:  int64(binary.BigEndian.Uint64(b[0:8])),
		expires: int64(binary.BigEndian.Uint64(b[8:16])),
	}
	copy(p.nonce[:], b[16:agentTokenFixedSize])
	return p, true
}

func (p agentTokenPayload) claims() AgentTokenClaims {
	return AgentTokenClaims{
		AgentID:   p.agentID,
		IssuedAt:  time.Unix(0, p.issued).UTC(),
		ExpiresAt: time.Unix(0, p.expires).UTC(),
	}
}
