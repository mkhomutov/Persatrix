package accounts

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// createPasswordAccount provisions an active account whose hash really
// derives from `password` under testParams.
func createPasswordAccount(t *testing.T, s *Store, username, password string) *Account {
	t.Helper()
	hash, err := HashPassword(password, testParams)
	require.NoError(t, err)
	acct, err := s.CreateAccount(context.Background(), NewAccount{
		Username:      username,
		PasswordHash:  hash,
		ParticipantID: username + "-h",
	})
	require.NoError(t, err)
	return acct
}

func TestPasswordAuthenticator_Success(t *testing.T) {
	s := openStore(t)
	acct := createPasswordAccount(t, s, "alice", "correct horse battery staple")

	auth := NewPasswordAuthenticator(s, testParams)
	id, err := auth.Authenticate(context.Background(), Credentials{
		Username: "alice", Password: "correct horse battery staple"})
	require.NoError(t, err)
	assert.Equal(t, acct.ID, id)
}

func TestPasswordAuthenticator_CaseFoldsUsername(t *testing.T) {
	s := openStore(t)
	acct := createPasswordAccount(t, s, "alice", "pw-1")

	auth := NewPasswordAuthenticator(s, testParams)
	id, err := auth.Authenticate(context.Background(), Credentials{
		Username: "  ALICE  ", Password: "pw-1"})
	require.NoError(t, err)
	assert.Equal(t, acct.ID, id, "login folds the username the way the write path does")
}

func TestPasswordAuthenticator_WrongPassword(t *testing.T) {
	s := openStore(t)
	createPasswordAccount(t, s, "alice", "pw-1")

	auth := NewPasswordAuthenticator(s, testParams)
	_, err := auth.Authenticate(context.Background(), Credentials{
		Username: "alice", Password: "pw-2"})
	assert.ErrorIs(t, err, ErrInvalidCredentials)
}

func TestPasswordAuthenticator_UnknownUsername(t *testing.T) {
	s := openStore(t)
	auth := NewPasswordAuthenticator(s, testParams)

	_, err := auth.Authenticate(context.Background(), Credentials{
		Username: "nobody", Password: "pw-1"})
	assert.ErrorIs(t, err, ErrInvalidCredentials,
		"§C account-existence non-disclosure: unknown user and wrong password are the same error")
	assert.NotErrorIs(t, err, ErrAccountNotFound)
}

func TestPasswordAuthenticator_DisabledAccount(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()
	acct := createPasswordAccount(t, s, "alice", "pw-1")
	require.NoError(t, s.SetAccountStatus(ctx, acct.ID, StatusDisabled))

	auth := NewPasswordAuthenticator(s, testParams)

	// The credential is checked first: only a caller holding the correct
	// password may learn the account is disabled.
	_, err := auth.Authenticate(ctx, Credentials{Username: "alice", Password: "pw-2"})
	assert.ErrorIs(t, err, ErrInvalidCredentials)

	_, err = auth.Authenticate(ctx, Credentials{Username: "alice", Password: "pw-1"})
	assert.ErrorIs(t, err, ErrAccountDisabled)
}

// TestPasswordAuthenticator_NonPasswordAccount exercises the §I seam
// account through the same temporary-allowlist trick as the store tests:
// an account whose method is not `password` cannot password-login, and
// the failure is indistinguishable from bad credentials.
func TestPasswordAuthenticator_NonPasswordAccount(t *testing.T) {
	validAuthMethods["test-idp"] = true
	defer delete(validAuthMethods, "test-idp")

	s := openStore(t)
	_, err := s.CreateAccount(context.Background(), NewAccount{
		Username: "carol", AuthMethod: "test-idp", ParticipantID: "carol-h"})
	require.NoError(t, err)

	auth := NewPasswordAuthenticator(s, testParams)
	_, err = auth.Authenticate(context.Background(), Credentials{
		Username: "carol", Password: "pw-1"})
	assert.ErrorIs(t, err, ErrInvalidCredentials)
}

func TestPasswordAuthenticator_RehashOnVerify(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	// Hash under yesterday's cost, authenticate under today's.
	oldParams := Params{MemoryKiB: 512, Iterations: 1, Parallelism: 1}
	oldHash, err := HashPassword("pw-1", oldParams)
	require.NoError(t, err)
	acct, err := s.CreateAccount(ctx, NewAccount{
		Username: "alice", PasswordHash: oldHash, ParticipantID: "alice-h"})
	require.NoError(t, err)

	auth := NewPasswordAuthenticator(s, testParams)
	id, err := auth.Authenticate(ctx, Credentials{Username: "alice", Password: "pw-1"})
	require.NoError(t, err)
	assert.Equal(t, acct.ID, id)

	// §C verify-then-rehash: the stored credential now carries current
	// params, and still verifies.
	got, err := s.GetAccount(ctx, acct.ID)
	require.NoError(t, err)
	assert.NotEqual(t, oldHash, got.PasswordHash)
	stale, err := NeedsRehash(got.PasswordHash, testParams)
	require.NoError(t, err)
	assert.False(t, stale)
	ok, err := VerifyPassword("pw-1", got.PasswordHash)
	require.NoError(t, err)
	assert.True(t, ok)
}

// TestPasswordAuthenticator_BurnsKDFWorkOnNoAccountPaths pins the §C
// timing non-disclosure structurally: the paths that never reach a real
// hash (unknown username, non-password account) must still burn a full
// KDF derivation. Ratio-based against an in-process baseline, so it
// holds on any machine speed: without DummyVerify these paths are a
// single SELECT, orders of magnitude under the floor.
func TestPasswordAuthenticator_BurnsKDFWorkOnNoAccountPaths(t *testing.T) {
	validAuthMethods["test-idp"] = true
	defer delete(validAuthMethods, "test-idp")

	s := openStore(t)
	ctx := context.Background()

	// Heavy enough that one derivation dominates any store lookup.
	heavy := Params{MemoryKiB: 16384, Iterations: 3, Parallelism: 1}
	start := time.Now()
	_, err := HashPassword("pw-1", heavy)
	require.NoError(t, err)
	baseline := time.Since(start)

	_, err = s.CreateAccount(ctx, NewAccount{
		Username: "carol", AuthMethod: "test-idp", ParticipantID: "carol-h"})
	require.NoError(t, err)

	auth := NewPasswordAuthenticator(s, heavy)
	for name, creds := range map[string]Credentials{
		"unknown username":     {Username: "nobody", Password: "pw-1"},
		"non-password account": {Username: "carol", Password: "pw-1"},
	} {
		start = time.Now()
		_, err := auth.Authenticate(ctx, creds)
		elapsed := time.Since(start)
		assert.ErrorIs(t, err, ErrInvalidCredentials)
		assert.Greater(t, elapsed, baseline/4,
			"%s must burn real KDF work (§C) — is DummyVerify missing?", name)
	}
}

// TestAuthenticator_LoginPathEndsInASession is the §I seam contract:
// whatever the method, every login path ends in "issue a session" —
// Authenticate yields the accountID that IssueSession + ResolveSession
// round-trip.
func TestAuthenticator_LoginPathEndsInASession(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()
	acct := createPasswordAccount(t, s, "alice", "pw-1")

	var auth Authenticator = NewPasswordAuthenticator(s, testParams)
	id, err := auth.Authenticate(ctx, Credentials{Username: "alice", Password: "pw-1"})
	require.NoError(t, err)

	token, _, err := s.IssueSession(ctx, id, time.Hour)
	require.NoError(t, err)
	_, gotAcct, err := s.ResolveSession(ctx, token)
	require.NoError(t, err)
	assert.Equal(t, acct.ID, gotAcct.ID)
	assert.Equal(t, acct.ParticipantID, gotAcct.ParticipantID,
		"the resolved identity carries the §A participant binding")
}
