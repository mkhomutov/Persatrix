package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/accounts"
)

// RFC 0039 PR 4 — the §G `account bootstrap` subcommand (bootstrap.go):
// dispatch, the OQ 1 12-character floor, the confirm prompt, and the
// zero-accounts refusal, all through the injected password-prompt seam.

// fakePrompt returns each answer in turn; a call past the end fails the
// test (the subcommand asked for more input than the flow defines).
func fakePrompt(t *testing.T, answers ...string) passwordPromptFunc {
	t.Helper()
	i := 0
	return func(string) (string, error) {
		if i >= len(answers) {
			t.Fatal("unexpected extra password prompt")
		}
		a := answers[i]
		i++
		return a, nil
	}
}

// cheapSecurityYAML writes a security.yaml at the loader's 8 MiB Argon
// floor so bootstrap tests hash in milliseconds, not at production cost.
func cheapSecurityYAML(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	yaml := "auth:\n  password:\n    argon2_memory_kib: 8192\n    argon2_iterations: 1\n    argon2_parallelism: 1\n"
	require.NoError(t, os.WriteFile(filepath.Join(dir, "security.yaml"), []byte(yaml), 0o600))
	return dir
}

func bootstrapArgs(cfgDir, dbPath string, extra ...string) []string {
	return append([]string{
		"--username", "Root-Op",
		"--accounts-db", dbPath,
		"--config", cfgDir,
	}, extra...)
}

func TestAccountBootstrap_CreatesFirstOperator(t *testing.T) {
	cfgDir := cheapSecurityYAML(t)
	dbPath := filepath.Join(t.TempDir(), "data", "accounts.db")
	var out, errOut bytes.Buffer
	const pw = "a long enough password"

	code := runAccountBootstrap(bootstrapArgs(cfgDir, dbPath),
		fakePrompt(t, pw, pw), &out, &errOut)
	require.Equal(t, 0, code, "stderr: %s", errOut.String())
	assert.Contains(t, out.String(), "root-op", "success line names the folded username")
	assert.NotContains(t, out.String(), pw, "the password is never echoed")
	assert.NotContains(t, errOut.String(), pw, "the password is never echoed")

	store, err := accounts.Open(dbPath)
	require.NoError(t, err)
	defer store.Close()
	acct, err := store.GetAccountByUsername(context.Background(), "root-op")
	require.NoError(t, err)
	assert.Equal(t, accounts.RoleOperator, acct.Role)
	assert.Equal(t, accounts.StatusActive, acct.Status)
	assert.Equal(t, "root-op", acct.ParticipantID,
		"participant defaults to the folded username")

	ok, err := accounts.VerifyPassword(pw, acct.PasswordHash)
	require.NoError(t, err)
	assert.True(t, ok, "the stored hash verifies against the typed password")
}

func TestAccountBootstrap_ExplicitParticipantFlag(t *testing.T) {
	cfgDir := cheapSecurityYAML(t)
	dbPath := filepath.Join(t.TempDir(), "accounts.db")
	var out, errOut bytes.Buffer
	const pw = "a long enough password"

	code := runAccountBootstrap(
		bootstrapArgs(cfgDir, dbPath, "--participant", "maksim-h"),
		fakePrompt(t, pw, pw), &out, &errOut)
	require.Equal(t, 0, code, "stderr: %s", errOut.String())

	store, err := accounts.Open(dbPath)
	require.NoError(t, err)
	defer store.Close()
	acct, err := store.GetAccountByUsername(context.Background(), "root-op")
	require.NoError(t, err)
	assert.Equal(t, "maksim-h", acct.ParticipantID)
}

func TestAccountBootstrap_PasswordFloor(t *testing.T) {
	cfgDir := cheapSecurityYAML(t)
	dbPath := filepath.Join(t.TempDir(), "accounts.db")
	var out, errOut bytes.Buffer

	// Eleven characters — one under the OQ 1 floor. Rejected before the
	// confirm prompt (only one answer is consumed).
	code := runAccountBootstrap(bootstrapArgs(cfgDir, dbPath),
		fakePrompt(t, "elevenchars"), &out, &errOut)
	assert.Equal(t, 1, code)
	assert.Contains(t, errOut.String(), "12 characters")
	assert.NoFileExists(t, dbPath, "a refused bootstrap must not create the store")
}

func TestAccountBootstrap_ConfirmMismatch(t *testing.T) {
	cfgDir := cheapSecurityYAML(t)
	dbPath := filepath.Join(t.TempDir(), "accounts.db")
	var out, errOut bytes.Buffer

	code := runAccountBootstrap(bootstrapArgs(cfgDir, dbPath),
		fakePrompt(t, "a long enough password", "a different password!"), &out, &errOut)
	assert.Equal(t, 1, code)
	assert.Contains(t, errOut.String(), "do not match")
	assert.NoFileExists(t, dbPath)
}

func TestAccountBootstrap_RefusesSecondBootstrap(t *testing.T) {
	cfgDir := cheapSecurityYAML(t)
	dbPath := filepath.Join(t.TempDir(), "accounts.db")
	const pw = "a long enough password"

	var out, errOut bytes.Buffer
	require.Equal(t, 0, runAccountBootstrap(bootstrapArgs(cfgDir, dbPath),
		fakePrompt(t, pw, pw), &out, &errOut), "stderr: %s", errOut.String())

	out.Reset()
	errOut.Reset()
	code := runAccountBootstrap(bootstrapArgs(cfgDir, dbPath),
		fakePrompt(t, pw, pw), &out, &errOut)
	assert.Equal(t, 1, code)
	assert.Contains(t, errOut.String(), "already exist")
}

func TestAccountBootstrap_UsernameRequired(t *testing.T) {
	var out, errOut bytes.Buffer
	code := runAccountBootstrap(
		[]string{"--accounts-db", filepath.Join(t.TempDir(), "accounts.db")},
		fakePrompt(t), &out, &errOut)
	assert.Equal(t, 2, code)
	assert.Contains(t, errOut.String(), "--username")
}

func TestAccountBootstrap_MalformedSecurityYAMLFailsLoud(t *testing.T) {
	// The same loud-fail posture as server startup: a present-but-broken
	// security.yaml must not silently hash under default parameters.
	cfgDir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(cfgDir, "security.yaml"),
		[]byte("auth:\n  mode: enbaled\n"), 0o600))
	dbPath := filepath.Join(t.TempDir(), "accounts.db")
	var out, errOut bytes.Buffer

	code := runAccountBootstrap(bootstrapArgs(cfgDir, dbPath),
		fakePrompt(t), &out, &errOut)
	assert.Equal(t, 1, code)
	assert.NoFileExists(t, dbPath)
}

func TestRunSubcommand_Dispatch(t *testing.T) {
	// Server invocations (flags, or nothing) never dispatch — main()
	// falls through to flag.Parse() and the ordinary boot path.
	for _, args := range [][]string{{}, {"--http-port", "8081"}, {"-config", "config/"}} {
		_, ok := runSubcommand(args)
		assert.False(t, ok, "args %v must not dispatch", args)
	}

	// An unknown account verb errs (exit 2) rather than booting a server
	// that the operator plainly did not ask for.
	code, ok := runSubcommand([]string{"account"})
	assert.True(t, ok)
	assert.Equal(t, 2, code)
	code, ok = runSubcommand([]string{"account", "delete"})
	assert.True(t, ok)
	assert.Equal(t, 2, code)
}

func TestAccountBootstrap_UnexpectedPositionalArgs(t *testing.T) {
	var out, errOut bytes.Buffer
	code := runAccountBootstrap([]string{"--username", "op", "extra"},
		fakePrompt(t), &out, &errOut)
	assert.Equal(t, 2, code)
}
