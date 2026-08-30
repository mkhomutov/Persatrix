package accounts

import (
	"context"
	"errors"
	"fmt"
	"unicode/utf8"
)

// First-operator bootstrap (RFC 0039 §G) — the store half of the
// `persatrix-server account bootstrap` subcommand. Account creation is
// operator-gated (§E), but a fresh install has no operator; this
// resolves the chicken-and-egg locally, against the filesystem, instead
// of via a network-reachable unauthenticated account-creation endpoint.

// MinBootstrapPasswordLen is the amendment OQ 1 resolution (2026-07-29):
// a 12-character floor on the first operator's password, enforced at the
// bootstrap subcommand. The full password-strength policy stays Phase 3.
const MinBootstrapPasswordLen = 12

// ErrAccountsExist — the §G zero-accounts precondition failed: the store
// already holds at least one account, so bootstrap refuses. It can never
// add a second operator or take over an existing install.
var ErrAccountsExist = errors.New("accounts: accounts already exist — bootstrap refused")

// ValidateBootstrapPassword enforces the bootstrap password floor. The
// floor counts characters (runes), not bytes — a multi-byte passphrase
// must not pass on encoding length alone.
func ValidateBootstrapPassword(password string) error {
	if n := utf8.RuneCountInString(password); n < MinBootstrapPasswordLen {
		return fmt.Errorf("accounts: bootstrap password must be at least %d characters (got %d)",
			MinBootstrapPasswordLen, n)
	}
	return nil
}

// BootstrapFirstAccount creates the first account, running the §G
// zero-accounts precondition and the insert in one transaction so the
// check cannot race another writer: any existing account — whatever its
// role or status — refuses the bootstrap with [ErrAccountsExist].
//
// Validation is [buildAccount], byte-identical to CreateAccount's; the
// caller (the subcommand) chooses the role — §G always passes operator.
func (s *Store) BootstrapFirstAccount(ctx context.Context, in NewAccount) (*Account, error) {
	a, err := buildAccount(in)
	if err != nil {
		return nil, err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("accounts: bootstrap begin: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after Commit
	var n int
	if err := tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM accounts`).Scan(&n); err != nil {
		return nil, fmt.Errorf("accounts: bootstrap count: %w", err)
	}
	if n > 0 {
		return nil, ErrAccountsExist
	}
	if err := insertAccount(ctx, tx, a); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("accounts: bootstrap commit: %w", err)
	}
	return a, nil
}
