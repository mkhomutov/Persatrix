package accounts

import (
	"context"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// §G: BootstrapFirstAccount runs the zero-accounts precondition and the
// insert in ONE transaction, refuses when any account exists, and can
// therefore never add a second operator or take over an install.

func bootstrapNew() NewAccount {
	return NewAccount{
		Username:      "Root-Op",
		PasswordHash:  phc,
		Role:          RoleOperator,
		ParticipantID: "root-op",
	}
}

func TestBootstrapFirstAccount_CreatesOperatorOnEmptyStore(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	a, err := s.BootstrapFirstAccount(ctx, bootstrapNew())
	require.NoError(t, err)
	assert.Equal(t, RoleOperator, a.Role)
	assert.Equal(t, "root-op", a.Username, "username case-folds on write")

	got, err := s.GetAccountByUsername(ctx, "root-op")
	require.NoError(t, err)
	assert.Equal(t, a.ID, got.ID)
	assert.Equal(t, StatusActive, got.Status)
}

func TestBootstrapFirstAccount_RefusesWhenAnyAccountExists(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	// Any pre-existing account — even a plain user — must refuse the
	// bootstrap: it is FIRST-operator creation, not account creation.
	_, err := s.CreateAccount(ctx, NewAccount{
		Username:      "existing",
		PasswordHash:  phc,
		Role:          RoleUser,
		ParticipantID: "existing",
	})
	require.NoError(t, err)

	_, err = s.BootstrapFirstAccount(ctx, bootstrapNew())
	assert.ErrorIs(t, err, ErrAccountsExist)

	// The refused bootstrap must not have written a row.
	_, err = s.GetAccountByUsername(ctx, "root-op")
	assert.ErrorIs(t, err, ErrAccountNotFound)
}

func TestBootstrapFirstAccount_SecondBootstrapRefused(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	_, err := s.BootstrapFirstAccount(ctx, bootstrapNew())
	require.NoError(t, err)

	in := bootstrapNew()
	in.Username = "second-op"
	in.ParticipantID = "second-op"
	_, err = s.BootstrapFirstAccount(ctx, in)
	assert.ErrorIs(t, err, ErrAccountsExist)
}

func TestBootstrapFirstAccount_ValidatesLikeCreateAccount(t *testing.T) {
	s := openStore(t)
	ctx := context.Background()

	in := bootstrapNew()
	in.ParticipantID = "Not Valid!"
	_, err := s.BootstrapFirstAccount(ctx, in)
	assert.Error(t, err, "invalid participant_id must be rejected")

	in = bootstrapNew()
	in.PasswordHash = ""
	_, err = s.BootstrapFirstAccount(ctx, in)
	assert.Error(t, err, "password method without a hash must be rejected")
}

func TestBootstrapFirstAccount_SurvivesReopen(t *testing.T) {
	// The refusal must be durable state, not process memory: reopening
	// the same file still refuses.
	path := filepath.Join(t.TempDir(), "accounts.db")
	s, err := Open(path)
	require.NoError(t, err)
	_, err = s.BootstrapFirstAccount(context.Background(), bootstrapNew())
	require.NoError(t, err)
	require.NoError(t, s.Close())

	s2, err := Open(path)
	require.NoError(t, err)
	defer s2.Close()
	_, err = s2.BootstrapFirstAccount(context.Background(), bootstrapNew())
	assert.ErrorIs(t, err, ErrAccountsExist)
}

// OQ 1 (amendment, resolved 2026-07-29): a 12-character floor on the
// bootstrap password, enforced at the subcommand via this validator.
func TestValidateBootstrapPassword(t *testing.T) {
	cases := []struct {
		name string
		pw   string
		ok   bool
	}{
		{"empty", "", false},
		{"eleven ascii", "abcdefghijk", false},
		{"twelve ascii", "abcdefghijkl", true},
		{"long passphrase", "correct horse battery staple", true},
		// The floor counts characters, not bytes: eleven multi-byte
		// runes must not pass on byte length.
		{"eleven runes multibyte", strings.Repeat("ü", 11), false},
		{"twelve runes multibyte", strings.Repeat("ü", 12), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateBootstrapPassword(tc.pw)
			if tc.ok {
				assert.NoError(t, err)
			} else {
				assert.Error(t, err)
			}
		})
	}
}
