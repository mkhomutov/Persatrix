package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/term"

	"github.com/mkhomutov/persatrix/internal/accounts"
	"github.com/mkhomutov/persatrix/internal/server"
)

// RFC 0039 §G — `persatrix-server account bootstrap`: first-operator
// creation as an orchestrator-binary subcommand. Account creation is
// operator-gated (§E), but a fresh install has no operator; bootstrap
// opens accounts.db DIRECTLY on the local filesystem — reusing the same
// schema, migration, and Argon2id wrapper the running server uses, so
// the credential write path stays single-sourced in Go — and refuses if
// any account already exists (zero-accounts check + insert in one
// transaction, in the store). The password is prompted, never in argv
// and never environment-seeded: env vars sit in process listings and
// compose files in plaintext, which §G explicitly rejects.

// passwordPromptFunc is the credential-input seam: the production
// implementation reads without echo from the terminal; tests inject
// canned answers.
type passwordPromptFunc func(prompt string) (string, error)

// runSubcommand dispatches subcommand invocations that run INSTEAD of
// the server. Called before flag.Parse() so the server's global flags
// never see subcommand argv. ok=false means "not a subcommand" — main
// proceeds with the ordinary boot path.
func runSubcommand(args []string) (code int, ok bool) {
	if len(args) == 0 || args[0] != "account" {
		return 0, false
	}
	if len(args) < 2 || args[1] != "bootstrap" {
		fmt.Fprintln(os.Stderr, "usage: persatrix-server account bootstrap [--username <name>] [flags]")
		return 2, true
	}
	return runAccountBootstrap(args[2:], promptPassword, os.Stdout, os.Stderr), true
}

// runAccountBootstrap is the testable core of the subcommand. Exit
// codes: 0 created, 1 refused/failed, 2 usage error.
func runAccountBootstrap(args []string, prompt passwordPromptFunc, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("account bootstrap", flag.ContinueOnError)
	fs.SetOutput(stderr)
	username := fs.String("username", "", "login name for the first operator (required)")
	participant := fs.String("participant", "", "participant id the account acts as (default: the folded username)")
	dbPath := fs.String("accounts-db", "data/accounts.db", "SQLite path for the accounts store")
	cfgDir := fs.String("config", "config/", "configuration directory (security.yaml supplies the Argon2id cost)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "account bootstrap: unexpected argument %q\n", fs.Arg(0))
		return 2
	}
	if strings.TrimSpace(*username) == "" {
		fmt.Fprintln(stderr, "account bootstrap: --username is required")
		fs.Usage()
		return 2
	}

	// Same loud-fail posture as server startup (auth.go): absent file →
	// defaults; present-but-malformed → refuse, so a broken config can
	// never silently hash the first credential under the wrong cost.
	authCfg, err := server.LoadSecurityConfig(filepath.Join(*cfgDir, "security.yaml"))
	if err != nil {
		fmt.Fprintf(stderr, "account bootstrap: %v\n", err)
		return 1
	}

	pw, err := prompt(fmt.Sprintf("Password for %q (min %d characters): ",
		accounts.FoldUsername(*username), accounts.MinBootstrapPasswordLen))
	if err != nil {
		fmt.Fprintf(stderr, "account bootstrap: read password: %v\n", err)
		return 1
	}
	// The OQ 1 floor, checked before the confirm prompt so a too-short
	// password fails in one round trip.
	if err := accounts.ValidateBootstrapPassword(pw); err != nil {
		fmt.Fprintf(stderr, "account bootstrap: %v\n", err)
		return 1
	}
	confirm, err := prompt("Confirm password: ")
	if err != nil {
		fmt.Fprintf(stderr, "account bootstrap: read password: %v\n", err)
		return 1
	}
	if confirm != pw {
		fmt.Fprintln(stderr, "account bootstrap: passwords do not match")
		return 1
	}

	hash, err := accounts.HashPassword(pw, authCfg.Argon)
	if err != nil {
		fmt.Fprintf(stderr, "account bootstrap: %v\n", err)
		return 1
	}

	if dir := filepath.Dir(*dbPath); dir != "." && dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			fmt.Fprintf(stderr, "account bootstrap: %v\n", err)
			return 1
		}
	}
	store, err := accounts.Open(*dbPath)
	if err != nil {
		fmt.Fprintf(stderr, "account bootstrap: %v\n", err)
		return 1
	}
	defer store.Close() //nolint:errcheck // read-only teardown on a one-shot process

	pid := *participant
	if pid == "" {
		pid = accounts.FoldUsername(*username)
	}
	a, err := store.BootstrapFirstAccount(context.Background(), accounts.NewAccount{
		Username:      *username,
		PasswordHash:  hash,
		Role:          accounts.RoleOperator,
		ParticipantID: pid,
	})
	if err != nil {
		if errors.Is(err, accounts.ErrAccountsExist) {
			fmt.Fprintln(stderr, "account bootstrap: accounts already exist — bootstrap creates only the FIRST operator (§G); further accounts are Phase 3 operator-gated administration")
			return 1
		}
		fmt.Fprintf(stderr, "account bootstrap: %v\n", err)
		return 1
	}
	fmt.Fprintf(stdout, "created operator account %q (participant %q) in %s\n",
		a.Username, a.ParticipantID, *dbPath)
	return 0
}

// stdinReader is shared across both prompts on the piped-stdin path: a
// fresh bufio.Reader per prompt would buffer past the first line and
// swallow the confirm line.
var stdinReader *bufio.Reader

// promptPassword reads a password without echo from the terminal (§J
// discipline — never argv, never env). When stdin is not a terminal (a
// provisioning pipe: `printf 'pw\npw\n' | persatrix-server account
// bootstrap …`), it reads one line per prompt instead; the prompt text
// goes to stderr either way so stdout stays parseable.
func promptPassword(prompt string) (string, error) {
	fmt.Fprint(os.Stderr, prompt)
	fd := int(os.Stdin.Fd())
	if term.IsTerminal(fd) {
		b, err := term.ReadPassword(fd)
		fmt.Fprintln(os.Stderr)
		return string(b), err
	}
	if stdinReader == nil {
		stdinReader = bufio.NewReader(os.Stdin)
	}
	line, err := stdinReader.ReadString('\n')
	if err != nil && line == "" {
		return "", err
	}
	fmt.Fprintln(os.Stderr)
	return strings.TrimRight(line, "\r\n"), nil
}
