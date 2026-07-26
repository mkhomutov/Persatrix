package accounts

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func openStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "accounts.db"))
	require.NoError(t, err)
	t.Cleanup(func() { _ = s.Close() })
	return s
}

// phc is a syntactically-real stored credential for CRUD tests; CRUD
// never runs the KDF, so a fixed cheap string keeps these tests fast.
const phc = "$argon2id$v=19$m=1024,t=1,p=1$c2FsdHNhbHRzYWx0c2FsdA$vJQxbUqUuia2E4GtLGWgOfXO7ItzS8xtnO8sfYHkpTM"

func validNew() NewAccount {
	return NewAccount{
		Username:      "alice",
		PasswordHash:  phc,
		Role:          RoleOperator,
		ParticipantID: "alice-h",
	}
}

func TestCreateAccount_GetRoundTrip(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	created, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)
	assert.NotEmpty(t, created.ID)
	assert.Equal(t, "alice", created.Username)
	assert.Equal(t, AuthMethodPassword, created.AuthMethod, "empty AuthMethod defaults to password")
	assert.Equal(t, StatusActive, created.Status, "every account starts active")
	assert.False(t, created.CreatedAt.IsZero())
	assert.Equal(t, created.CreatedAt, created.UpdatedAt)

	got, err := s.GetAccount(ctx, created.ID)
	require.NoError(t, err)
	assert.Equal(t, created, got)
}

func TestCreateAccount_Defaults(t *testing.T) {
	s := openStore(t)
	in := validNew()
	in.Role = ""
	created, err := s.CreateAccount(context.Background(), in)
	require.NoError(t, err)
	assert.Equal(t, RoleUser, created.Role, "empty Role defaults to user")
}

func TestGetAccountByUsername_CaseFolds(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	in := validNew()
	in.Username = "  Alice  " // §B: case-folded (and trimmed) on write
	created, err := s.CreateAccount(ctx, in)
	require.NoError(t, err)
	assert.Equal(t, "alice", created.Username, "the stored form is the folded form")

	got, err := s.GetAccountByUsername(ctx, "ALICE")
	require.NoError(t, err)
	assert.Equal(t, created.ID, got.ID, "lookups fold the same way writes do")
}

func TestCreateAccount_DuplicateUsername_IsErrUsernameTaken(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	_, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)

	dup := validNew()
	dup.Username = "ALICE" // distinct only by case — must still collide
	dup.ParticipantID = "alice-2"
	_, err = s.CreateAccount(ctx, dup)
	assert.ErrorIs(t, err, ErrUsernameTaken)
}

func TestCreateAccount_DuplicateParticipant_IsErrParticipantBound(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	_, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)

	dup := validNew()
	dup.Username = "bob"
	_, err = s.CreateAccount(ctx, dup) // same participant_id
	assert.ErrorIs(t, err, ErrParticipantBound,
		"the §A 1:1 binding is a schema UNIQUE constraint, surfaced as a sentinel")
}

func TestCreateAccount_WriteBoundaryValidation(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	for name, mutate := range map[string]func(*NewAccount){
		"empty username":              func(a *NewAccount) { a.Username = "" },
		"whitespace-only username":    func(a *NewAccount) { a.Username = "   " },
		"username with inner space":   func(a *NewAccount) { a.Username = "al ice" },
		"username with unicode space": func(a *NewAccount) { a.Username = "al\u00a0ice" }, // NBSP survives TrimSpace
		"username with C1 control":    func(a *NewAccount) { a.Username = "al\u009cice" },
		"unknown role":                func(a *NewAccount) { a.Role = "superadmin" },
		"unknown auth_method":         func(a *NewAccount) { a.AuthMethod = "oidc" },
		"empty participant_id":        func(a *NewAccount) { a.ParticipantID = "" },
		"uppercase participant_id":    func(a *NewAccount) { a.ParticipantID = "Alice" },
		"hyphen-edge participant_id":  func(a *NewAccount) { a.ParticipantID = "-alice" },
		"single-char participant_id":  func(a *NewAccount) { a.ParticipantID = "a" },
		"password method without PHC": func(a *NewAccount) { a.PasswordHash = "" },
	} {
		t.Run(name, func(t *testing.T) {
			in := validNew()
			mutate(&in)
			_, err := s.CreateAccount(ctx, in)
			assert.Error(t, err)
		})
	}

	n, err := s.CountAccounts(ctx)
	require.NoError(t, err)
	assert.Zero(t, n, "no rejected write may leave a row behind")
}

// TestCreateAccount_NonPasswordMethod_StoresNullHash exercises the §B
// "password_hash is NULL for non-password methods" rule through a
// temporary allowlist entry — the §I seam a future IdP authenticator
// will use. White-box: the allowlist map is package-level by design
// (the RFC 0016 extensible-string pattern).
func TestCreateAccount_NonPasswordMethod_StoresNullHash(t *testing.T) {
	validAuthMethods["test-idp"] = true
	defer delete(validAuthMethods, "test-idp")

	s := openStore(t)
	ctx := context.Background()

	in := validNew()
	in.AuthMethod = "test-idp"
	_, err := s.CreateAccount(ctx, in)
	assert.Error(t, err, "a non-password method must not carry a password hash")

	in.PasswordHash = ""
	created, err := s.CreateAccount(ctx, in)
	require.NoError(t, err)
	assert.Empty(t, created.PasswordHash)

	var isNull bool
	require.NoError(t, s.db.QueryRow(
		`SELECT password_hash IS NULL FROM accounts WHERE id = ?`, created.ID).Scan(&isNull))
	assert.True(t, isNull, "at rest the column must be NULL, not empty string (§B)")

	got, err := s.GetAccount(ctx, created.ID)
	require.NoError(t, err)
	assert.Empty(t, got.PasswordHash)

	// The update path enforces §B the same way the create path does: a
	// non-password account can never acquire a hash.
	err = s.SetPasswordHash(ctx, created.ID, phc)
	assert.Error(t, err)
	assert.NotErrorIs(t, err, ErrAccountNotFound, "the account exists; the method is what is wrong")
	require.NoError(t, s.db.QueryRow(
		`SELECT password_hash IS NULL FROM accounts WHERE id = ?`, created.ID).Scan(&isNull))
	assert.True(t, isNull, "the rejected update must not have written a hash")
}

func TestSetAccountStatus(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	created, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)

	require.NoError(t, s.SetAccountStatus(ctx, created.ID, StatusDisabled))
	got, err := s.GetAccount(ctx, created.ID)
	require.NoError(t, err)
	assert.Equal(t, StatusDisabled, got.Status)
	assert.False(t, got.UpdatedAt.Before(created.UpdatedAt))

	assert.Error(t, s.SetAccountStatus(ctx, created.ID, "deleted"),
		"statuses outside the allowlist are rejected — accounts are disabled, never deleted (§K)")
	assert.ErrorIs(t, s.SetAccountStatus(ctx, "no-such-id", StatusDisabled), ErrAccountNotFound)
}

func TestSetPasswordHash(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	created, err := s.CreateAccount(ctx, validNew())
	require.NoError(t, err)

	rehashed := "$argon2id$v=19$m=2048,t=2,p=2$c2FsdHNhbHRzYWx0c2FsdA$vJQxbUqUuia2E4GtLGWgOfXO7ItzS8xtnO8sfYHkpTM"
	require.NoError(t, s.SetPasswordHash(ctx, created.ID, rehashed))
	got, err := s.GetAccount(ctx, created.ID)
	require.NoError(t, err)
	assert.Equal(t, rehashed, got.PasswordHash)

	assert.Error(t, s.SetPasswordHash(ctx, created.ID, ""))
	assert.Error(t, s.SetPasswordHash(ctx, created.ID, "not-a-phc-string"),
		"only a well-formed argon2id PHC string may be stored (§B)")
	assert.ErrorIs(t, s.SetPasswordHash(ctx, "no-such-id", phc), ErrAccountNotFound)
}

func TestListAndCountAccounts(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	n, err := s.CountAccounts(ctx)
	require.NoError(t, err)
	assert.Zero(t, n)

	list, err := s.ListAccounts(ctx)
	require.NoError(t, err)
	assert.Empty(t, list)

	for _, u := range []string{"alice", "bob", "carol"} {
		in := NewAccount{Username: u, PasswordHash: phc, ParticipantID: u + "-h"}
		_, err := s.CreateAccount(ctx, in)
		require.NoError(t, err)
	}

	n, err = s.CountAccounts(ctx)
	require.NoError(t, err)
	assert.Equal(t, 3, n)

	list, err = s.ListAccounts(ctx)
	require.NoError(t, err)
	require.Len(t, list, 3)
	var usernames []string
	for _, a := range list {
		usernames = append(usernames, a.Username)
	}
	assert.ElementsMatch(t, []string{"alice", "bob", "carol"}, usernames)
	for i := 1; i < len(list); i++ {
		assert.False(t, list[i].CreatedAt.Before(list[i-1].CreatedAt), "oldest first")
	}
}

func TestGetAccount_Unknown_IsErrAccountNotFound(t *testing.T) {
	s := openStore(t)
	_, err := s.GetAccount(context.Background(), "no-such-id")
	assert.ErrorIs(t, err, ErrAccountNotFound)
	_, err = s.GetAccountByUsername(context.Background(), "nobody")
	assert.ErrorIs(t, err, ErrAccountNotFound)
}

func TestTimestamps_AreUTCSecondPrecision(t *testing.T) {
	s := openStore(t)
	created, err := s.CreateAccount(context.Background(), validNew())
	require.NoError(t, err)

	assert.Equal(t, time.UTC, created.CreatedAt.Location())
	assert.Zero(t, created.CreatedAt.Nanosecond(), "INTEGER-at-rest timestamps are second precision")

	got, err := s.GetAccount(context.Background(), created.ID)
	require.NoError(t, err)
	assert.True(t, got.CreatedAt.Equal(created.CreatedAt), "what was written reads back exactly")
}
