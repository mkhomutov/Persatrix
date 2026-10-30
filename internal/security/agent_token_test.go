package security

// Tests for agent tokens (RFC 0009 Phases 3–4 plan, PR 5): what a token
// holds, which strings are refused, which IDs get one, what a check costs,
// and that the key never leaves the issuer. Time, revocation and forgetting
// are in agent_token_revocation_test.go. Both sit inside the package, not in
// security_test, because some must see what no caller can: the signed fields,
// to change one at a time; the issuer's clocks, to move them apart; and its
// list of agents, to prove it shrinks.

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"runtime/debug"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// raceDetector reports whether the race detector is built in. It reads the
// test binary's build settings rather than a file behind `//go:build race`:
// the repository's runner check counts a file behind a build line as one
// `go test ./...` never runs.
var raceDetector = func() bool {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return false
	}
	for _, s := range info.Settings {
		if s.Key == "-race" {
			return s.Value == "true"
		}
	}
	return false
}()

// tokenClockStart is a real date, so issue times read as one.
var tokenClockStart = time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)

// tokenClock is an issuer's two clocks for a test (see [agentTokenClock]).
// Time passing moves both. A time sync steps only the time of day, and so
// does a machine's sleep, since the running clock stops while it sleeps.
type tokenClock struct {
	mu      sync.Mutex
	wall    time.Time
	running time.Duration
	// tick moves both clocks on after every read, so time passes while
	// goroutines use one issuer at once.
	tick time.Duration
}

func newTokenClock(wall time.Time) *tokenClock { return &tokenClock{wall: wall} }

func (c *tokenClock) read() (int64, time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	wall, running := c.wall.UnixNano(), c.running
	c.wall, c.running = c.wall.Add(c.tick), c.running+c.tick
	return wall, running
}

// pass moves both clocks on by d.
func (c *tokenClock) pass(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.wall, c.running = c.wall.Add(d), c.running+d
}

// stepWall moves only the time of day, as a time sync or a sleep does.
func (c *tokenClock) stepWall(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.wall = c.wall.Add(d)
}

func newTestKey() []byte {
	key := make([]byte, agentTokenKeySize)
	_, _ = rand.Read(key)
	return key
}

func newTestIssuer(clk *tokenClock) *AgentTokenIssuer {
	return newAgentTokenIssuer(newTestKey(), clk.read)
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
	issuer := newTestIssuer(newTokenClock(tokenClockStart))

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

	other := issueToken(t, issuer, "tide-wren")
	first, _ := fieldsOf(t, token)
	second, _ := fieldsOf(t, other)
	assert.Less(t, first.serial, second.serial, "each token has its own serial number, higher for a later one")
	assert.Equal(t, first.issued+int64(AgentTokenTTL), first.expires)
	assert.Equal(t, AgentTokenTTL, first.runningExpires, "on the running clock, which starts at zero, it expires 24 hours in")
}

func TestAgentTokenIssuer_RefusesATokenWithAnyFieldChanged(t *testing.T) {
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
	token := issueToken(t, issuer, "ember-owl")
	fields, mac := fieldsOf(t, token)
	encoded := base64.RawURLEncoding.EncodeToString(fields.marshal(nil))

	rebuild := func(p agentTokenPayload) string {
		return agentTokenPrefix + base64.RawURLEncoding.EncodeToString(p.marshal(nil)) + "." + mac
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
		{"running expiry", func(p *agentTokenPayload) { p.runningExpires += time.Hour }},
		{"serial number", func(p *agentTokenPayload) { p.serial++ }},
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

// Every one-character change is refused, wherever it falls. The last
// character of a base64 part carries spare bits, and a base64 decoder skips
// line breaks, so a signature read loosely would let a few changes through.
func TestAgentTokenIssuer_RefusesATokenWithAnyCharacterChanged(t *testing.T) {
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
	token := issueToken(t, issuer, "ember-owl")
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.=\r\n"

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
	for i := range len(token) + 1 {
		for _, c := range []string{"\r", "\n", "A"} {
			changed := token[:i] + c + token[i:]
			if _, err := issuer.Check(changed); err == nil {
				admitted = append(admitted, changed)
			}
		}
	}
	for _, changed := range []string{token[1:], token[:len(token)-1]} {
		if _, err := issuer.Check(changed); err == nil {
			admitted = append(admitted, changed)
		}
	}
	assert.Empty(t, admitted, "a changed token was admitted")

	// A base64 decoder skips line breaks, so a signature part that ends in
	// one decodes one byte short. Compared at full length, the decode buffer
	// would stand in for the missing byte, and the decoder leaves it zero,
	// so the change would pass for a token whose signature ends in a zero
	// byte: about one token in 256.
	for i := 0; ; i++ {
		require.Less(t, i, 1<<14, "no signature ended in a zero byte")
		token := issueToken(t, issuer, fmt.Sprintf("ember-owl-%d", i))
		_, signature := fieldsOf(t, token)
		sig, err := base64.RawURLEncoding.DecodeString(signature)
		require.NoError(t, err)
		if sig[len(sig)-1] != 0 {
			continue
		}
		_, err = issuer.Check(token[:len(token)-1] + "\n")
		assert.ErrorIs(t, err, ErrAgentTokenInvalid, "a signature one byte short, with a line break for its last character")
		break
	}
}

func TestAgentTokenIssuer_RefusesATokenFromAnotherProcess(t *testing.T) {
	before := NewAgentTokenIssuer() // the orchestrator before a restart
	after := NewAgentTokenIssuer()  // the same orchestrator after it
	token := issueToken(t, before, "ember-owl")

	_, err := after.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenInvalid, "a restart makes a new key, so every earlier token stops working")
	_, err = before.Check(token)
	assert.NoError(t, err)
}

func TestAgentTokenIssuer_RefusesWhatIsNotAToken(t *testing.T) {
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
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
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
	// A real token, lengthened, so only the length stops it before the
	// signature is worked out.
	overlong := issueToken(t, issuer, "ember-owl") + strings.Repeat("A", maxAgentTokenLen)
	var err error
	allocs := testing.AllocsPerRun(10, func() { _, err = issuer.Check(overlong) })
	assert.ErrorIs(t, err, ErrAgentTokenInvalid)
	assert.Zero(t, allocs, "an overlong token was read before it was refused")
}

// Check runs on every call an agent makes, so a token that checks costs one
// allocation: the agent ID it returns (PR 5 review F-19).
func TestAgentTokenIssuer_ChecksATokenWithOneAllocation(t *testing.T) {
	if raceDetector {
		t.Skip("the race detector drops pooled values at random, so a check allocates anew")
	}
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
	token := issueToken(t, issuer, strings.Repeat("e", MaxAgentIDLen))
	var err error
	allocs := testing.AllocsPerRun(100, func() { _, err = issuer.Check(token) })
	require.NoError(t, err)
	assert.LessOrEqual(t, allocs, 1.0)
}

// A token signed with the issuer's key for an agent it holds nothing about
// cannot be one it issued, since it forgets an agent only once all the
// agent's tokens have expired. Such a token is refused, not trusted.
func TestAgentTokenIssuer_RefusesASignedTokenForAnAgentItNeverIssuedTo(t *testing.T) {
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
	forged := agentTokenPayload{
		agentID:        "tide-wren",
		issued:         tokenClockStart.UnixNano(),
		expires:        tokenClockStart.Add(AgentTokenTTL).UnixNano(),
		runningExpires: AgentTokenTTL,
		serial:         1,
	}
	m := issuer.macs.Get().(*agentTokenMAC)
	defer issuer.macs.Put(m)
	_, err := issuer.Check(m.sign(forged))
	assert.ErrorIs(t, err, ErrAgentTokenInvalid)
}

func TestAgentTokenIssuer_IssuesOnlyForAnIDAnAgentCanHave(t *testing.T) {
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
	for name, id := range map[string]string{
		"empty":          "",
		"upper case":     "Ember-Owl",
		"one character":  "e",
		"leading hyphen": "-ember",
		"a space":        "ember owl",
		"over the limit": strings.Repeat("e", MaxAgentIDLen+1),
		// The security layer treats these two specially, so a token
		// naming either would pass for the console or for every caller
		// that names no agent (PR 5 review F-11).
		"the web console":       ConsoleAgentID,
		"the anonymous callers": DefaultUnauthenticatedID,
	} {
		token, issued, err := issuer.Issue(id)
		assert.ErrorIs(t, err, ErrAgentTokenAgentID, name)
		assert.Empty(t, token, name)
		assert.Zero(t, issued, name)
	}
	assert.Empty(t, issuer.agents, "a refused ID leaves nothing behind")

	longest := strings.Repeat("e", MaxAgentIDLen)
	token := issueToken(t, issuer, longest)
	assert.Equal(t, maxAgentTokenLen, len(token), "the longest ID's token is the longest Check reads")
	got, err := issuer.Check(token)
	require.NoError(t, err)
	assert.Equal(t, longest, got.AgentID)
}

// recordingT takes testify's failure messages, so a test can read what a
// failing assertion would print.
type recordingT struct{ messages []string }

func (r *recordingT) Errorf(format string, args ...any) {
	r.messages = append(r.messages, fmt.Sprintf(format, args...))
}

// The key never leaves the issuer: not through a log line that names the
// issuer, its errors or a token, nor through printing or encoding it, nor
// through a value that holds it, nor through testify's diff of two such
// values, which follows pointers into unexported fields (PR 5 review F-16).
func TestAgentTokenIssuer_KeyReachesNoLogLineTokenOrError(t *testing.T) {
	key := newTestKey()
	issuer := newAgentTokenIssuer(bytes.Clone(key), systemAgentTokenClock())
	decimal := strings.Trim(fmt.Sprintf("%d", key), "[]")
	forms := map[string]string{
		"raw":        string(key),
		"hex":        hex.EncodeToString(key),
		"a hex dump": fmt.Sprintf("% x", key[:8]), // how go-spew prints bytes
		"base64":     base64.StdEncoding.EncodeToString(key),
		"base64url":  base64.RawURLEncoding.EncodeToString(key),
		"decimal":    decimal,                               // how fmt prints bytes
		"a list":     strings.ReplaceAll(decimal, " ", ","), // how JSON writes a byte array
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
	var diff recordingT
	assert.Equal(&diff, held, server{NewAgentTokenIssuer()})
	require.NotEmpty(t, diff.messages, "the two values differ, so testify printed them")

	for name, out := range map[string]string{
		"log line":     line.String(),
		"json":         string(encoded),
		"printed":      fmt.Sprintf("%v %+v %#v", issuer, issuer, issuer),
		"held":         fmt.Sprintf("%v %+v %#v", held, held, held),
		"testify diff": strings.Join(diff.messages, "\n"),
		"token":        token,
		"errors":       invalid.Error() + revoked.Error(),
	} {
		for form, secret := range forms {
			assert.NotContains(t, out, secret, "the %s carries the key as %s", name, form)
		}
	}
}
