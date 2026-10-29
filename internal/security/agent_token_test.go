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
	assert.Equal(t, AgentTokenClaims{
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

	// The clock the issuer is given steps back an hour. time.Now's
	// monotonic reading never does, but the issuer does not rely on that.
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
		"empty":                 "",
		"prefix only":           agentTokenPrefix,
		"no signature":          token[:lastDot],
		"an empty signature":    token[:lastDot+1],
		"an extra part":         token + ".AAAA",
		"a space before it":     " " + token,
		"a bearer header":       "Bearer " + token,
		"longer than any token": token + strings.Repeat("A", maxAgentTokenLen),
		"an account session":    base64.RawURLEncoding.EncodeToString(make([]byte, 32)),
	} {
		_, err := issuer.Check(bad)
		assert.ErrorIs(t, err, ErrAgentTokenInvalid, name)
	}
}

// The length is checked before anything else, so a long string costs
// nothing to refuse (PR 5 review F-2).
func TestAgentTokenIssuer_RefusesAnOverlongTokenBeforeAnyWork(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))
	// A real token, lengthened, so only the length stops it before the
	// signature is worked out.
	overlong := issueToken(t, issuer, "ember-owl") + strings.Repeat("A", maxAgentTokenLen)
	var err error
	allocs := testing.AllocsPerRun(10, func() { _, err = issuer.Check(overlong) })
	assert.ErrorIs(t, err, ErrAgentTokenInvalid)
	assert.Zero(t, allocs, "an overlong token was read before it was refused")
}

// A token signed with the issuer's key for an agent it holds nothing about
// cannot be one it issued, since it forgets an agent only once all the
// agent's tokens have expired. Such a token is refused, not trusted.
func TestAgentTokenIssuer_RefusesASignedTokenForAnAgentItNeverIssuedTo(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))
	p := agentTokenPayload{
		agentID: "tide-wren",
		issued:  tokenClockStart.UnixNano(),
		expires: tokenClockStart.Add(AgentTokenTTL).UnixNano(),
	}
	signed := agentTokenPrefix + base64.RawURLEncoding.EncodeToString(p.marshal())
	_, err := issuer.Check(signed + "." + issuer.sign(signed))
	assert.ErrorIs(t, err, ErrAgentTokenInvalid)
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

// issueUntilForgetting issues tokens to new agents, named from prefix, until
// the next Issue forgets the agents whose tokens have all expired, and then
// makes that Issue.
func issueUntilForgetting(t *testing.T, issuer *AgentTokenIssuer, prefix string) {
	t.Helper()
	for i := 0; len(issuer.agents) < issuer.sweepAt; i++ {
		issueToken(t, issuer, fmt.Sprintf("%s-%d", prefix, i))
	}
	issueToken(t, issuer, prefix+"-last")
}

func TestAgentTokenIssuer_ForgetsAnAgentOnceAllItsTokensHaveExpired(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	old := issueToken(t, issuer, "ember-owl")
	for range 3 {
		issuer.Revoke("ember-owl")
	}
	issuer.Revoke("never-issued")
	issuer.Revoke("Not An Agent")
	assert.Len(t, issuer.agents, 1, "one entry per agent holding a token; revoking again, or revoking an agent with none, keeps nothing more")

	clk.Advance(AgentTokenTTL)
	issueUntilForgetting(t, issuer, "later")
	assert.NotContains(t, issuer.agents, "ember-owl", "an agent whose tokens have all expired is forgotten")
	assert.LessOrEqual(t, len(issuer.agents), 2*agentTokenSweepFloor)
	_, err := issuer.Check(old)
	assert.ErrorIs(t, err, ErrAgentTokenExpired, "a forgotten agent's token is still refused")
}

// The issuer forgets in batches that grow with the map, so Issue does not
// walk every agent each time once there are many.
func TestAgentTokenIssuer_ForgetsOnlyOnceTheMapHasGrown(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(tokenClockStart))
	for i := range 3 * agentTokenSweepFloor {
		issueToken(t, issuer, fmt.Sprintf("agent-%d", i))
	}
	assert.Greater(t, issuer.sweepAt, len(issuer.agents), "with every token still working, the next pass waits for the map to grow")
}

// A machine without a clock battery can read 1970 until it syncs; a token
// issued at that first instant still works.
func TestAgentTokenIssuer_WorksFromTheFirstInstantOfAClockAt1970(t *testing.T) {
	issuer := newTestIssuer(newFakeClock(time.Unix(0, 0)))
	token, issued, err := issuer.Issue("ember-owl")
	require.NoError(t, err)
	require.Zero(t, issued.IssuedAt.UnixNano())
	_, err = issuer.Check(token)
	assert.NoError(t, err)
}

func TestAgentTokenIssuer_KeepsARevocationWhileAnyOfTheAgentsTokensWorks(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")
	clk.Advance(23 * time.Hour)
	later := issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")

	clk.Advance(2 * time.Hour) // past the first token's 24 hours, not the later one's
	issueUntilForgetting(t, issuer, "filler")
	_, err := issuer.Check(later)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked, "the agent, and its revocation, are kept while one of its tokens still works")
}

func TestAgentTokenIssuer_IsSafeForConcurrentUse(t *testing.T) {
	issuer := NewAgentTokenIssuer(AgentTokenConfig{})
	var wg sync.WaitGroup
	for i := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range 50 {
				// A new agent each time, so the map grows and Issue
				// forgets what has expired while the others run.
				id := fmt.Sprintf("agent-%d-%d", i, j)
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

// A revoked token stays refused even when the issuer's clock goes back after
// the issuer forgot the agent (PR 5 review F-1).
func TestAgentTokenIssuer_ARevokedTokenStaysRefusedWhenItsClockGoesBack(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	token := issueToken(t, issuer, "ember-owl")
	clk.Advance(time.Minute)
	issuer.Revoke("ember-owl")

	clk.Advance(AgentTokenTTL + time.Second)
	issueUntilForgetting(t, issuer, "filler")
	require.NotContains(t, issuer.agents, "ember-owl", "the issuer has forgotten the agent and its revocation")

	// The clock the issuer is given goes back two minutes, to before the
	// token expired. time.Now's monotonic reading never does, but the
	// issuer does not rely on that.
	clk.Advance(-2 * time.Minute)
	_, err := issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenExpired, "a revoked token came back when the clock went back")
}

// Check reads its time and the agent's revocation together, under its lock,
// so nothing can drop the revocation between the two (PR 5 review F-1).
func TestAgentTokenIssuer_CheckReadsItsTimeUnderItsLock(t *testing.T) {
	clk := newFakeClock(tokenClockStart)
	var issuer *AgentTokenIssuer
	checking, unlockedReads := false, 0
	issuer = NewAgentTokenIssuer(AgentTokenConfig{Now: func() time.Time {
		if checking && issuer.mu.TryLock() {
			issuer.mu.Unlock()
			unlockedReads++
		}
		return clk.Now()
	}})
	token := issueToken(t, issuer, "ember-owl")

	checking = true
	_, err := issuer.Check(token)
	checking = false
	require.NoError(t, err)
	assert.Zero(t, unlockedReads, "Check read the clock while a revocation could be dropped")
}
