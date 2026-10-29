package security

// Tests for agent tokens (RFC 0009 Phases 3–4 plan, PR 5). They sit inside
// the package, not in security_test, because some must see what no caller
// can: the key, to prove it never leaves; the signed fields, to change one at
// a time; and the revocation list, to prove it shrinks.

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// tokenClockStart is a real date, so issue times read as one.
var tokenClockStart = time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)

func newTestIssuer(clk *fakeClock) *AgentTokenIssuer {
	return NewAgentTokenIssuer(AgentTokenConfig{Now: clk.Now})
}

func issueToken(t *testing.T, issuer *AgentTokenIssuer, agentID string) string {
	t.Helper()
	token, _, err := issuer.Issue(agentID)
	require.NoError(t, err)
	return token
}

// fieldsOf reads a token's fields without checking its signature, and
// returns the signature as sent.
func fieldsOf(t *testing.T, token string) (agentTokenPayload, string) {
	t.Helper()
	encoded, signature, ok := strings.Cut(strings.TrimPrefix(token, agentTokenPrefix), ".")
	require.True(t, ok)
	raw, err := base64.RawURLEncoding.DecodeString(encoded)
	require.NoError(t, err)
	fields, ok := unmarshalAgentTokenPayload(raw)
	require.True(t, ok)
	return fields, signature
}

func TestAgentTokenIssuer_IssuesATokenThatChecks(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))

	token, issued, err := issuer.Issue("ember-owl")
	require.NoError(t, err)
	assert.Equal(t, 24*time.Hour, AgentTokenTTL, "a token lives 24 hours (G-5)")
	assert.Equal(t, AgentToken{
		AgentID:   "ember-owl",
		IssuedAt:  tokenClockStart,
		ExpiresAt: tokenClockStart.Add(AgentTokenTTL),
	}, issued)

	got, err := issuer.Check(token)
	require.NoError(t, err)
	assert.Equal(t, issued, got, "a token says what Issue said it would")

	again := issueToken(t, issuer, "ember-owl")
	assert.NotEqual(t, token, again, "no two tokens are alike, even for one agent at one moment")
	first, _ := fieldsOf(t, token)
	second, _ := fieldsOf(t, again)
	assert.NotEqual(t, first.nonce, second.nonce, "each token has its own random nonce")
	assert.NotEqual(t, [agentTokenNonceSize]byte{}, first.nonce)
	for _, tok := range []string{token, again} {
		_, err := issuer.Check(tok)
		assert.NoError(t, err, "an agent's earlier token keeps working beside its newer one")
	}
}

func TestAgentTokenIssuer_RefusesATokenWithAnyFieldChanged(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))
	token := issueToken(t, issuer, "ember-owl")
	fields, mac := fieldsOf(t, token)
	encoded := base64.RawURLEncoding.EncodeToString(fields.marshal())

	rebuild := func(p agentTokenPayload) string {
		return agentTokenPrefix + base64.RawURLEncoding.EncodeToString(p.marshal()) + "." + mac
	}
	_, err := issuer.Check(rebuild(fields))
	require.NoError(t, err, "the token rebuilt unchanged still checks, so each refusal below is the change's doing")

	for _, c := range []struct {
		field  string
		change func(*agentTokenPayload)
	}{
		{"agent id", func(p *agentTokenPayload) { p.agentID = "tide-wren" }},
		{"issue time", func(p *agentTokenPayload) { p.issued -= int64(time.Hour) }},
		{"expiry", func(p *agentTokenPayload) { p.expires += int64(time.Hour) }},
		{"nonce", func(p *agentTokenPayload) { p.nonce[0] ^= 1 }},
	} {
		t.Run(c.field, func(t *testing.T) {
			forged := fields
			c.change(&forged)
			_, err := issuer.Check(rebuild(forged))
			assert.ErrorIs(t, err, ErrAgentTokenInvalid)
		})
	}
	t.Run("signature", func(t *testing.T) {
		zero := base64.RawURLEncoding.EncodeToString(make([]byte, sha256.Size))
		_, err := issuer.Check(agentTokenPrefix + encoded + "." + zero)
		assert.ErrorIs(t, err, ErrAgentTokenInvalid)
	})
	t.Run("format version", func(t *testing.T) {
		_, err := issuer.Check("agt2." + encoded + "." + mac)
		assert.ErrorIs(t, err, ErrAgentTokenInvalid)
	})
}

// Every one-character change is refused, wherever it falls. A signature
// checked over the decoded bytes, rather than the text sent, would let a few
// changes through: the last character of a base64 part carries spare bits.
func TestAgentTokenIssuer_RefusesATokenWithAnyCharacterChanged(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))
	token := issueToken(t, issuer, "ember-owl")
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_."

	var admitted []string
	for i := range len(token) {
		for _, c := range []byte(alphabet) {
			if c == token[i] {
				continue
			}
			changed := token[:i] + string(c) + token[i+1:]
			if _, err := issuer.Check(changed); err == nil {
				admitted = append(admitted, changed)
			}
		}
	}
	for _, changed := range []string{token[1:], token[:len(token)-1], token + "A", "A" + token} {
		if _, err := issuer.Check(changed); err == nil {
			admitted = append(admitted, changed)
		}
	}
	assert.Empty(t, admitted, "a changed token was admitted")
}

func TestAgentTokenIssuer_RefusesAnExpiredToken(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	token := issueToken(t, issuer, "ember-owl")

	clk.Advance(AgentTokenTTL - time.Nanosecond)
	_, err := issuer.Check(token)
	require.NoError(t, err, "a token works until the last moment of its 24 hours")

	clk.Advance(time.Nanosecond)
	_, err = issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenExpired)

	fresh := issueToken(t, issuer, "ember-owl")
	_, err = issuer.Check(fresh)
	assert.NoError(t, err, "an agent whose token expired gets a working one by registering again")
}

func TestAgentTokenIssuer_RevokeRefusesEveryTokenTheAgentHolds(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	first := issueToken(t, issuer, "ember-owl")
	clk.Advance(time.Minute)
	second := issueToken(t, issuer, "ember-owl")
	other := issueToken(t, issuer, "tide-wren")

	issuer.Revoke("ember-owl")

	for _, token := range []string{first, second} {
		_, err := issuer.Check(token)
		assert.ErrorIs(t, err, ErrAgentTokenRevoked)
	}
	got, err := issuer.Check(other)
	require.NoError(t, err, "revoking one agent's tokens leaves another's working")
	assert.Equal(t, "tide-wren", got.AgentID)
}

func TestAgentTokenIssuer_ATokenIssuedAfterARevocationWorks(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	old := issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")

	// The clock reads what it read at the revocation, as when a
	// deregistered agent registers again at once.
	sameMoment := issueToken(t, issuer, "ember-owl")
	_, err := issuer.Check(sameMoment)
	assert.NoError(t, err, "a token issued after a revocation is not caught by it")

	// The clock steps back an hour, as a time sync can.
	clk.Advance(-time.Hour)
	issuer.Revoke("ember-owl")
	_, err = issuer.Check(sameMoment)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked, "a revocation after the step still catches every token issued before it")
	afterStep := issueToken(t, issuer, "ember-owl")
	_, err = issuer.Check(afterStep)
	assert.NoError(t, err, "a token issued after the step's revocation works")

	_, err = issuer.Check(old)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked)
}

func TestAgentTokenIssuer_RefusesATokenFromAnotherProcess(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	before := newTestIssuer(clk) // the orchestrator before a restart
	after := newTestIssuer(clk)  // the same orchestrator after it
	require.NotEqual(t, *before.key, *after.key, "each process makes its own key")
	token := issueToken(t, before, "ember-owl")

	_, err := after.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenInvalid, "a restart makes a new key, so every earlier token stops working")
	_, err = before.Check(token)
	assert.NoError(t, err)
}

func TestAgentTokenIssuer_RefusesWhatIsNotAToken(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))
	token := issueToken(t, issuer, "ember-owl")
	lastDot := strings.LastIndexByte(token, '.')

	for name, bad := range map[string]string{
		"empty":                   "",
		"prefix only":             agentTokenPrefix,
		"no signature":            token[:lastDot],
		"an empty signature":      token[:lastDot+1],
		"an extra part":           token + ".AAAA",
		"a space before it":       " " + token,
		"a bearer header":         "Bearer " + token,
		"over the longest length": token + strings.Repeat("A", maxAgentTokenLen),
		"an account session":      base64.RawURLEncoding.EncodeToString(make([]byte, 32)),
	} {
		_, err := issuer.Check(bad)
		assert.ErrorIs(t, err, ErrAgentTokenInvalid, name)
	}
}

func TestAgentTokenIssuer_IssuesOnlyForAnIDAnAgentCanHave(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))
	for name, id := range map[string]string{
		"empty":          "",
		"upper case":     "Ember-Owl",
		"one character":  "e",
		"leading hyphen": "-ember",
		"a space":        "ember owl",
		"over 256 bytes": strings.Repeat("e", MaxAgentIDLen+1),
	} {
		token, issued, err := issuer.Issue(id)
		assert.ErrorIs(t, err, ErrAgentTokenAgentID, name)
		assert.Empty(t, token, name)
		assert.Zero(t, issued, name)
	}

	longest := strings.Repeat("e", MaxAgentIDLen)
	token := issueToken(t, issuer, longest)
	assert.LessOrEqual(t, len(token), maxAgentTokenLen, "the longest ID's token is within the length Check reads")
	got, err := issuer.Check(token)
	require.NoError(t, err)
	assert.Equal(t, longest, got.AgentID)
}

func TestAgentTokenIssuer_ForgetsARevocationOnceItsTokensHaveExpired(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	old := issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")
	for i := range 100 {
		issuer.Revoke(fmt.Sprintf("agent-%d", i))
	}
	issuer.Revoke("ember-owl")
	issuer.Revoke("Not An Agent")
	assert.Len(t, issuer.revoked, 101, "one entry per agent, and none for an ID no agent can have")

	clk.Advance(AgentTokenTTL + time.Second)
	issuer.Revoke("tide-wren")
	assert.Len(t, issuer.revoked, 1, "a revocation older than a token's lifetime guards nothing, so it is dropped")
	assert.Len(t, issuer.order, 1)
	_, err := issuer.Check(old)
	assert.ErrorIs(t, err, ErrAgentTokenExpired, "a token whose revocation was dropped is still refused")
}

func TestAgentTokenIssuer_DroppingARevocationKeepsALaterOneForTheSameAgent(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	issuer.Revoke("ember-owl")
	clk.Advance(23 * time.Hour)
	token := issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")

	clk.Advance(2 * time.Hour) // past the first revocation's 24 hours, not the token's
	issuer.Revoke("tide-wren")
	_, err := issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked, "the later revocation still stands once the earlier one is dropped")
}

func TestAgentTokenIssuer_IsSafeForConcurrentUse(t *testing.T) {
	issuer := NewAgentTokenIssuer(AgentTokenConfig{})
	var wg sync.WaitGroup
	for i := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			id := fmt.Sprintf("agent-%d", i)
			for range 50 {
				token, _, err := issuer.Issue(id)
				if err != nil {
					t.Error(err)
					return
				}
				if _, err := issuer.Check(token); err != nil {
					t.Errorf("%s: a fresh token was refused: %v", id, err)
				}
				issuer.Revoke(id)
				if _, err := issuer.Check(token); !errors.Is(err, ErrAgentTokenRevoked) {
					t.Errorf("%s: a revoked token was not refused as revoked: %v", id, err)
				}
			}
		}()
	}
	wg.Wait()
}

// The key never leaves the issuer: not through a log line that names the
// issuer, its errors or a token, nor through printing or encoding it, nor
// through a value that holds it.
func TestAgentTokenIssuer_KeyReachesNoLogLineTokenOrError(t *testing.T) {
	issuer := NewAgentTokenIssuer(AgentTokenConfig{})
	raw := issuer.key[:]
	decimal := strings.Trim(fmt.Sprintf("%d", raw), "[]")
	forms := map[string]string{
		"raw":       string(raw),
		"hex":       hex.EncodeToString(raw),
		"base64":    base64.StdEncoding.EncodeToString(raw),
		"base64url": base64.RawURLEncoding.EncodeToString(raw),
		"decimal":   decimal,                               // how fmt prints bytes
		"a list":    strings.ReplaceAll(decimal, " ", ","), // how JSON writes a byte array
	}

	token := issueToken(t, issuer, "ember-owl")
	_, invalid := issuer.Check(token + "A")
	issuer.Revoke("ember-owl")
	_, revoked := issuer.Check(token)
	require.Error(t, invalid)
	require.Error(t, revoked)

	var line bytes.Buffer
	logger := zap.New(zapcore.NewCore(
		zapcore.NewJSONEncoder(zap.NewProductionEncoderConfig()), zapcore.AddSync(&line), zap.DebugLevel))
	logger.Info("agent tokens",
		zap.Any("issuer", issuer), zap.Reflect("reflected", issuer), zap.String("token", token),
		zap.NamedError("invalid", invalid), zap.NamedError("revoked", revoked))
	require.NoError(t, logger.Sync())
	encoded, err := json.Marshal(map[string]any{"issuer": issuer})
	require.NoError(t, err)
	type server struct{ tokens *AgentTokenIssuer }
	held := server{issuer}

	for name, out := range map[string]string{
		"log line": line.String(),
		"json":     string(encoded),
		"printed":  fmt.Sprintf("%v %+v %#v", issuer, issuer, issuer),
		"held":     fmt.Sprintf("%v %+v %#v", held, held, held),
		"token":    token,
		"errors":   invalid.Error() + revoked.Error(),
	} {
		for form, secret := range forms {
			assert.NotContains(t, out, secret, "the %s carries the key as %s", name, form)
		}
	}
}
