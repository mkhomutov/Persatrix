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
	"hash"
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

	agentTokenKeySize = 32 // an HMAC-SHA256 key

	// agentTokenFixedSize is the fields before the agent ID, 8 bytes each:
	// the issue time and the expiry by the time of day, the expiry by the
	// issuer's running clock, and the serial number.
	agentTokenFixedSize = 4 * 8
)

// agentTokenSweepFloor is how many agents the issuer holds before Issue
// first forgets the ones whose tokens have all expired.
const agentTokenSweepFloor = 64

// agentTokenEncoding writes both parts of a token. Strict decoding refuses
// a last character with spare bits set, so only one spelling of a signature
// decodes.
var agentTokenEncoding = base64.RawURLEncoding.Strict()

var (
	// agentTokenSignatureLen is the length of a token's signature part.
	agentTokenSignatureLen = agentTokenEncoding.EncodedLen(sha256.Size)
	// maxAgentTokenLen is the longest token Issue makes, the one for an ID
	// of [MaxAgentIDLen]. Check refuses anything longer before any work.
	maxAgentTokenLen = len(agentTokenPrefix) +
		agentTokenEncoding.EncodedLen(agentTokenFixedSize+MaxAgentIDLen) + 1 + agentTokenSignatureLen
)

// Why a token is refused. They stay apart for the audit line; what a caller
// is told is the server's choice.
var (
	// ErrAgentTokenInvalid: not a token this process issued. It is garbled,
	// changed, or signed with another key, such as the one before a restart.
	ErrAgentTokenInvalid = errors.New("security: agent token is not valid")
	// ErrAgentTokenExpired: the token is past its [AgentTokenTTL].
	ErrAgentTokenExpired = errors.New("security: agent token has expired")
	// ErrAgentTokenRevoked: the agent's tokens were revoked after this one
	// was issued, as when the agent deregistered, or it was issued a newer
	// one.
	ErrAgentTokenRevoked = errors.New("security: agent token has been revoked")
	// ErrAgentTokenAgentID: Issue was asked for an ID no agent can have:
	// empty, one the REST and gRPC hooks refuse as an [AgentIDHeader] (see
	// validAgentID), or one the security layer gives callers that are not
	// agents ([ConsoleAgentID], [DefaultUnauthenticatedID]).
	ErrAgentTokenAgentID = errors.New("security: no agent token can be issued for this agent ID")
)

// AgentTokenClaims is what a token says: which agent it was issued to, and
// when it was issued and expires by the orchestrator's clock.
type AgentTokenClaims struct {
	AgentID   string
	IssuedAt  time.Time
	ExpiresAt time.Time
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
// The fields are the agent's ID, when the token was issued and expires, and
// its serial number. The signature is HMAC-SHA256 over everything before it,
// under a key the issuer makes from crypto/rand and never stores or hands
// out. So nobody without the key can make a token or change one, and a
// restart stops every earlier token from working. Both parts are base64url
// without padding, so a token fits an HTTP header or gRPC metadata as it is.
// A token lists nothing the agent may do: nothing in v0.4.0 would read such
// a list.
//
// A token stops working when 24 hours have passed on either of two clocks
// (see [agentTokenClock]): the time of day, which a time sync can step but
// which counts the hours a machine sleeps, and the time the issuer has been
// running, which no time sync moves. So stepping the clock back cannot make
// a token last longer than 24 hours, and a machine's sleep cannot either.
// Its IssuedAt and ExpiresAt are times of day, as an audit line's are.
//
// Revocation goes by serial number, not by time: an agent's tokens up to a
// number are refused. Issuing a token refuses every earlier one the agent
// holds, and [AgentTokenIssuer.Revoke] refuses the latest too. Revocations
// are kept in this process only, since v0.4.0 runs one orchestrator, and a
// restart stops every token by making a new key.
type AgentTokenIssuer struct {
	// macs hands out HMACs under the key, kept for reuse. The key lives only
	// in the pool's New function and the HMACs it makes, so printing or
	// encoding the issuer, even by following pointers as go-spew does,
	// shows no key byte: a function prints as an address at most, and the
	// pool keeps what it holds behind unsafe pointers. No method returns it.
	macs  sync.Pool
	clock agentTokenClock

	mu sync.RWMutex
	// serial is the serial number of the latest token issued; the first
	// is 1.
	serial uint64
	// agents holds what the issuer knows of each agent while its latest
	// token may still work. Issue forgets the others once the map has
	// reached sweepAt.
	agents  map[string]agentTokenState
	sweepAt int
}

// agentTokenState is what the issuer keeps about one agent.
type agentTokenState struct {
	// revokedThrough is the highest serial number of its tokens that are
	// refused: every one before its latest, and the latest too once revoked.
	revokedThrough uint64
	// expires is when its latest token expires on the running clock.
	expires time.Duration
}

// agentTokenClock reads the two clocks an issuer keeps time by: the time of
// day, in Unix nanoseconds, and how long the issuer has been running.
type agentTokenClock func() (wall int64, running time.Duration)

// systemAgentTokenClock reads the computer's clocks. The running time comes
// from time.Now's monotonic reading: no time sync moves it, but on Linux and
// macOS it stops while the machine sleeps. Only this package can give an
// issuer another clock, so no caller can hand it a time.Now with that reading
// stripped, as time.Now().UTC() does.
func systemAgentTokenClock() agentTokenClock {
	start := time.Now()
	return func() (int64, time.Duration) {
		now := time.Now()
		return now.UnixNano(), now.Sub(start)
	}
}

// NewAgentTokenIssuer makes an issuer with a new key from crypto/rand.
func NewAgentTokenIssuer() *AgentTokenIssuer {
	key := make([]byte, agentTokenKeySize)
	// rand.Read never returns an error: if the system's random source
	// fails, it stops the program instead.
	_, _ = rand.Read(key)
	return newAgentTokenIssuer(key, systemAgentTokenClock())
}

func newAgentTokenIssuer(key []byte, clock agentTokenClock) *AgentTokenIssuer {
	t := &AgentTokenIssuer{
		clock:   clock,
		agents:  make(map[string]agentTokenState),
		sweepAt: agentTokenSweepFloor,
	}
	t.macs.New = func() any {
		return &agentTokenMAC{
			Hash:   hmac.New(sha256.New, key),
			text:   make([]byte, 0, maxAgentTokenLen),
			fields: make([]byte, 0, agentTokenFixedSize+MaxAgentIDLen),
		}
	}
	return t
}

// Issue makes a token for agentID that works for [AgentTokenTTL], and says
// what the token holds. The agent's earlier tokens stop working, so
// registering again shuts out a copy of an old one. An ID no agent can have
// gets [ErrAgentTokenAgentID].
func (t *AgentTokenIssuer) Issue(agentID string) (string, AgentTokenClaims, error) {
	if !issuableAgentID(agentID) {
		return "", AgentTokenClaims{}, ErrAgentTokenAgentID
	}
	p := agentTokenPayload{agentID: agentID}

	t.mu.Lock()
	wall, running := t.clock()
	t.serial++
	p.serial = t.serial
	p.issued, p.expires = wall, wall+int64(AgentTokenTTL)
	p.runningExpires = running + AgentTokenTTL
	if len(t.agents) >= t.sweepAt {
		t.forgetExpired(running)
	}
	t.agents[agentID] = agentTokenState{revokedThrough: p.serial - 1, expires: p.runningExpires}
	t.mu.Unlock()

	m := t.macs.Get().(*agentTokenMAC)
	defer t.macs.Put(m)
	return m.sign(p), p.claims(), nil
}

// Check says which agent a token was issued to, or why the token is refused:
// [ErrAgentTokenInvalid], [ErrAgentTokenRevoked] or [ErrAgentTokenExpired].
// The signature is checked first, against the token's text exactly as sent,
// and nothing else in the token is read unless it matches. A revoked token
// reads as revoked, not expired, for as long as the issuer holds its agent.
func (t *AgentTokenIssuer) Check(token string) (AgentTokenClaims, error) {
	if len(token) > maxAgentTokenLen || !strings.HasPrefix(token, agentTokenPrefix) {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}
	dot := strings.LastIndexByte(token, '.')
	if dot < len(agentTokenPrefix) || len(token)-dot-1 != agentTokenSignatureLen {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}
	m := t.macs.Get().(*agentTokenMAC)
	p, ok := m.verify(token, dot)
	t.macs.Put(m)
	if !ok {
		return AgentTokenClaims{}, ErrAgentTokenInvalid
	}

	// The time and the agent's state are read together, so nothing can
	// forget a revocation between the two.
	t.mu.RLock()
	wall, running := t.clock()
	state, known := t.agents[p.agentID]
	t.mu.RUnlock()
	switch {
	case known && p.serial <= state.revokedThrough:
		return AgentTokenClaims{}, ErrAgentTokenRevoked
	case wall >= p.expires || running >= p.runningExpires:
		return AgentTokenClaims{}, ErrAgentTokenExpired
	case !known:
		// The issuer forgets an agent only once its tokens have all
		// expired on the running clock, which never goes back, so an
		// unexpired token for an agent it does not know was not issued
		// here.
		return AgentTokenClaims{}, ErrAgentTokenInvalid
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
	if state, known := t.agents[agentID]; known {
		state.revokedThrough = t.serial
		t.agents[agentID] = state
	}
}

// forgetExpired drops each agent whose latest token has expired on the
// running clock by now: its earlier ones expired before it, so none can pass
// Check, and its revocation guards nothing. Issue calls it only once the map
// has doubled since the last time, so its cost, spread over the Issues that
// grew the map, stays the same however many agents there are, and the map
// stays within about twice the agents whose latest token still works. The
// caller holds t.mu for writing.
func (t *AgentTokenIssuer) forgetExpired(running time.Duration) {
	for id, state := range t.agents {
		if running >= state.expires {
			delete(t.agents, id)
		}
	}
	t.sweepAt = max(agentTokenSweepFloor, 2*len(t.agents))
}

// issuableAgentID reports whether an agent can have id: the REST and gRPC
// hooks accept it as an [AgentIDHeader], and it is none the security layer
// keeps for callers that are not agents. A token for [ConsoleAgentID] would
// pass for the web console, which the circuit breaker never quarantines, and
// one for [DefaultUnauthenticatedID] would share the rate limiter's bucket
// for every caller that names no agent.
func issuableAgentID(id string) bool {
	return id != "" && validAgentID(id) && id != ConsoleAgentID && id != DefaultUnauthenticatedID
}

// agentTokenMAC is an HMAC under the issuer's key, with room to make or
// check a token without allocating.
type agentTokenMAC struct {
	hash.Hash
	text     []byte // the token's text
	fields   []byte // its fields, decoded
	sum, sig [sha256.Size]byte
}

// sign returns the token for p.
func (m *agentTokenMAC) sign(p agentTokenPayload) string {
	m.fields = p.marshal(m.fields[:0])
	m.text = agentTokenEncoding.AppendEncode(append(m.text[:0], agentTokenPrefix...), m.fields)
	m.Reset()
	m.Write(m.text)
	m.text = agentTokenEncoding.AppendEncode(append(m.text, '.'), m.Sum(m.sum[:0]))
	return string(m.text)
}

// verify reports whether the signature after the token's last dot, at dot,
// is the one for the text before it, and if so reads the token's fields. A
// base64 decoder skips line breaks, so a signature part holding one decodes
// short of a full signature, and is refused for its length.
func (m *agentTokenMAC) verify(token string, dot int) (agentTokenPayload, bool) {
	m.text = append(m.text[:0], token...)
	signed := m.text[:dot]
	n, err := agentTokenEncoding.Decode(m.sig[:], m.text[dot+1:])
	if err != nil {
		return agentTokenPayload{}, false
	}
	m.Reset()
	m.Write(signed)
	// hmac.Equal takes the same time wherever two signatures of one length
	// differ, so the time a refusal takes cannot help anyone guess one.
	if !hmac.Equal(m.Sum(m.sum[:0]), m.sig[:n]) {
		return agentTokenPayload{}, false
	}
	n, err = agentTokenEncoding.Decode(m.fields[:cap(m.fields)], signed[len(agentTokenPrefix):])
	if err != nil {
		return agentTokenPayload{}, false
	}
	return unmarshalAgentTokenPayload(m.fields[:n])
}

// agentTokenPayload is a token's fields, which its signature covers.
type agentTokenPayload struct {
	agentID        string
	issued         int64         // Unix nanoseconds
	expires        int64         // Unix nanoseconds
	runningExpires time.Duration // on the issuer's running clock
	serial         uint64
}

// marshal appends the fields to b as a token carries them: the issue time,
// the expiry, the running expiry and the serial number as 8-byte big-endian
// integers, then the agent ID.
func (p agentTokenPayload) marshal(b []byte) []byte {
	b = binary.BigEndian.AppendUint64(b, uint64(p.issued))
	b = binary.BigEndian.AppendUint64(b, uint64(p.expires))
	b = binary.BigEndian.AppendUint64(b, uint64(p.runningExpires))
	b = binary.BigEndian.AppendUint64(b, p.serial)
	return append(b, p.agentID...)
}

// unmarshalAgentTokenPayload reads what marshal wrote, and reports false
// for bytes too short to hold an agent ID.
func unmarshalAgentTokenPayload(b []byte) (agentTokenPayload, bool) {
	if len(b) <= agentTokenFixedSize {
		return agentTokenPayload{}, false
	}
	return agentTokenPayload{
		agentID:        string(b[agentTokenFixedSize:]),
		issued:         int64(binary.BigEndian.Uint64(b[0:8])),
		expires:        int64(binary.BigEndian.Uint64(b[8:16])),
		runningExpires: time.Duration(binary.BigEndian.Uint64(b[16:24])),
		serial:         binary.BigEndian.Uint64(b[24:32]),
	}, true
}

func (p agentTokenPayload) claims() AgentTokenClaims {
	return AgentTokenClaims{
		AgentID:   p.agentID,
		IssuedAt:  time.Unix(0, p.issued).UTC(),
		ExpiresAt: time.Unix(0, p.expires).UTC(),
	}
}
