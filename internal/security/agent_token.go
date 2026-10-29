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
	// ErrAgentTokenAgentID: Issue was asked for an ID no agent can have, one
	// the REST and gRPC hooks would refuse as an X-Agent-ID.
	ErrAgentTokenAgentID = errors.New("security: an agent token's agent ID must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$ and be at most 256 bytes")
)

// AgentToken is what a token says: which agent it was issued to, and when it
// was issued and expires.
type AgentToken struct {
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
// Revocations are kept in this process only, since v0.4.0 runs one
// orchestrator: [AgentTokenIssuer.Revoke] stops every token one agent holds,
// and a restart stops all of them by making a new key.
type AgentTokenIssuer struct {
	// key signs every token. It sits behind a pointer, so printing the
	// issuer, or a value that holds one, shows an address rather than the
	// key; no method returns it.
	key *[agentTokenKeySize]byte
	now func() time.Time

	mu sync.RWMutex
	// last is the latest moment Issue or Revoke recorded, in Unix
	// nanoseconds (see [AgentTokenIssuer.moment]).
	last int64
	// revoked maps each agent whose tokens were revoked to the moment of
	// its latest revocation. order holds the same revocations, oldest first,
	// so the ones that no longer guard anything can be dropped.
	revoked map[string]int64
	order   []agentTokenRevocation
}

type agentTokenRevocation struct {
	agentID string
	at      int64
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
		last:    math.MinInt64,
		revoked: make(map[string]int64),
	}
}

// Issue makes a token for agentID that works for [AgentTokenTTL], and says
// what the token holds. An ID no agent can have gets [ErrAgentTokenAgentID].
func (t *AgentTokenIssuer) Issue(agentID string) (string, AgentToken, error) {
	if agentID == "" || !validAgentID(agentID) {
		return "", AgentToken{}, ErrAgentTokenAgentID
	}
	p := agentTokenPayload{agentID: agentID}
	_, _ = rand.Read(p.nonce[:]) // never fails; see NewAgentTokenIssuer
	t.mu.Lock()
	p.issued = t.moment()
	t.mu.Unlock()
	p.expires = p.issued + int64(AgentTokenTTL)

	signed := agentTokenPrefix + base64.RawURLEncoding.EncodeToString(p.marshal())
	return signed + "." + t.sign(signed), p.agentToken(), nil
}

// Check says which agent a token was issued to, or why the token is refused:
// [ErrAgentTokenInvalid], [ErrAgentTokenExpired] or [ErrAgentTokenRevoked].
// The signature is checked first, against the token's text exactly as sent,
// and nothing else in the token is read unless it matches.
func (t *AgentTokenIssuer) Check(token string) (AgentToken, error) {
	if len(token) > maxAgentTokenLen {
		return AgentToken{}, ErrAgentTokenInvalid
	}
	dot := strings.LastIndexByte(token, '.')
	if dot < len(agentTokenPrefix) || !strings.HasPrefix(token, agentTokenPrefix) {
		return AgentToken{}, ErrAgentTokenInvalid
	}
	signed, signature := token[:dot], token[dot+1:]
	// hmac.Equal takes the same time wherever two signatures differ, so the
	// time a refusal takes cannot help anyone guess a signature.
	if !hmac.Equal([]byte(signature), []byte(t.sign(signed))) {
		return AgentToken{}, ErrAgentTokenInvalid
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(signed[len(agentTokenPrefix):])
	if err != nil {
		return AgentToken{}, ErrAgentTokenInvalid
	}
	p, ok := unmarshalAgentTokenPayload(raw)
	if !ok {
		return AgentToken{}, ErrAgentTokenInvalid
	}

	if t.now().UnixNano() >= p.expires {
		return AgentToken{}, ErrAgentTokenExpired
	}
	t.mu.RLock()
	revokedAt, found := t.revoked[p.agentID]
	t.mu.RUnlock()
	if found && p.issued <= revokedAt {
		return AgentToken{}, ErrAgentTokenRevoked
	}
	return p.agentToken(), nil
}

// Revoke stops every token issued to agentID so far, as deregistering the
// agent needs. A token issued to it afterwards works, so the agent can
// register again. An ID no agent can have holds no token, so it is ignored.
func (t *AgentTokenIssuer) Revoke(agentID string) {
	if agentID == "" || !validAgentID(agentID) {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.forget(t.now().UnixNano())
	at := t.moment()
	t.revoked[agentID] = at
	t.order = append(t.order, agentTokenRevocation{agentID: agentID, at: at})
}

// moment returns the clock's reading in Unix nanoseconds, or a nanosecond
// past the last moment it returned if the clock has not moved past that one.
// So each moment is later than the one before, even when the clock stands
// still or a time sync steps it back, and a token issued after a revocation
// always has a later moment than the revocation, which therefore never
// catches it. After a step back, a new token's issue time is ahead of the
// clock, and the token works for that much longer. The caller holds t.mu.
func (t *AgentTokenIssuer) moment() int64 {
	t.last = max(t.now().UnixNano(), t.last+1)
	return t.last
}

// forget drops each revocation that no longer guards anything. A revocation
// at moment m catches only tokens issued before m, and each of those has
// expired by m + AgentTokenTTL, so from then on Check refuses them as expired
// anyway. Revocations are recorded in the order of their moments, so the
// ones to drop are at the front. This keeps the list to the revocations of
// the last [AgentTokenTTL]. The caller holds t.mu.
func (t *AgentTokenIssuer) forget(now int64) {
	for len(t.order) > 0 && now >= t.order[0].at+int64(AgentTokenTTL) {
		r := t.order[0]
		if t.revoked[r.agentID] == r.at {
			delete(t.revoked, r.agentID)
		}
		t.order[0] = agentTokenRevocation{} // lets the ID be freed
		t.order = t.order[1:]
	}
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

func (p agentTokenPayload) agentToken() AgentToken {
	return AgentToken{
		AgentID:   p.agentID,
		IssuedAt:  time.Unix(0, p.issued).UTC(),
		ExpiresAt: time.Unix(0, p.expires).UTC(),
	}
}
