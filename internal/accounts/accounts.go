// Package accounts owns the orchestrator's durable account store —
// `accounts.db`, the second orchestrator-owned SQLite database after
// `channels.db` (RFC 0039 §A/§B).
//
// An Account answers "is this connecting client entitled to act as that
// someone" — it is NOT the RFC 0016 UserParticipant, which answers "who
// is this, socially, to the agents" and lives in a persona's memory.db.
// The two are joined by a documentary `participant_id` binding, 1:1 in
// the foundation and enforced by a schema UNIQUE constraint, so
// authentication proves the account and the system then acts as the
// bound participant (§A).
//
// This package is deliberately leaf-shaped: it depends on nothing else
// in-module, so the `persatrix-server account bootstrap` subcommand
// (RFC 0039 §G) and the REST auth surface can both reuse the same
// schema, migration, and Argon2id KDF without a dependency cycle.
package accounts

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
)

// Role values. `role` is an extensible TEXT column validated at the
// write boundary — the allowlist-validated-string pattern RFC 0016
// chose for `participant_type` (RFC 0039 §B). A new role is a one-line
// change here, no migration.
const (
	RoleOperator = "operator"
	RoleUser     = "user"
)

// AuthMethod values (§I extension seam). `password` is the only method
// the foundation implements; the column exists from the first migration
// so a federated (OIDC/SAML) account is representable the day an IdP
// authenticator lands.
const (
	AuthMethodPassword = "password"
)

// Status values. Accounts are never deleted — only disabled (§K), which
// is why `sessions.account_id → accounts.id` needs no ON DELETE clause.
const (
	StatusActive   = "active"
	StatusDisabled = "disabled"
)

var (
	validRoles       = map[string]bool{RoleOperator: true, RoleUser: true}
	validAuthMethods = map[string]bool{AuthMethodPassword: true}
	validStatuses    = map[string]bool{StatusActive: true, StatusDisabled: true}
)

// participantIDRegex is the existing RFC 0016 participant-ID shape,
// verbatim from RFC 0039 §A: the binding is a valid identity by
// construction. The bound participant need not exist yet — a
// UserParticipant is created lazily on first chat interaction, so an
// operator routinely provisions the account first.
var participantIDRegex = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*[a-z0-9]$`)

// maxUsernameLen bounds the case-folded login name. Generous — the REST
// layer's MaxBytesReader is the real request cap; this is store-side
// hygiene so a pathological value cannot become a durable row.
const maxUsernameLen = 256

// Sentinel errors returned by store CRUD. Callers branch with
// errors.Is; the wrapped driver error (where any) stays in the chain.
var (
	// ErrAccountNotFound — no account matches the id / username.
	ErrAccountNotFound = errors.New("accounts: account not found")
	// ErrUsernameTaken — the case-folded username is already registered
	// (schema UNIQUE on `username`).
	ErrUsernameTaken = errors.New("accounts: username already taken")
	// ErrParticipantBound — another account already binds this
	// participant_id; the §A 1:1 invariant is a schema UNIQUE
	// constraint, not application bookkeeping.
	ErrParticipantBound = errors.New("accounts: participant already bound to an account")
)

// Account is one row of `accounts` (§B): a login credential and its
// coarse role, bound to the RFC 0016 participant it is authorized to
// act as.
type Account struct {
	ID            string    // account UUID
	Username      string    // login name; case-folded on write
	AuthMethod    string    // §I seam; 'password' in the foundation
	PasswordHash  string    // argon2id PHC string; empty for non-password methods
	Role          string    // 'operator' | 'user'
	ParticipantID string    // 1:1 binding to the UserParticipant (§A)
	Status        string    // 'active' | 'disabled'
	CreatedAt     time.Time // UTC, second precision (INTEGER at rest)
	UpdatedAt     time.Time // UTC, second precision (INTEGER at rest)
}

// Session is one row of `sessions` (§B/§D): a server-side opaque
// session, persisted token-hash-only so a read of accounts.db yields no
// usable live credential. The model lands with the schema in PR 1; the
// session store (issue / resolve / expire / revoke / prune) is the next
// PR's.
type Session struct {
	TokenHash  string     // sha256(opaque token), hex; the raw token is never stored
	AccountID  string     // -> accounts.id (FK enforced; PRAGMA foreign_keys=ON)
	IssuedAt   time.Time  // UTC, second precision
	ExpiresAt  time.Time  // UTC, second precision
	LastUsedAt time.Time  // refreshed lazily (§D), so coarse by design
	RevokedAt  *time.Time // nil while live
}

// FoldUsername canonicalizes a login name the way every write and
// lookup does: trimmed, lowercased. §B says "case-folded on write";
// strings.ToLower is the deliberate, stable fold — usernames are
// operator-provisioned identifiers, not free prose, and a
// locale-sensitive fold would let two visually-distinct names collide
// differently across binaries.
func FoldUsername(username string) string {
	return strings.ToLower(strings.TrimSpace(username))
}

// validateUsername checks the already-folded name at the write
// boundary.
func validateUsername(folded string) error {
	if folded == "" {
		return fmt.Errorf("accounts: username must be non-empty")
	}
	if len(folded) > maxUsernameLen {
		return fmt.Errorf("accounts: username exceeds %d bytes", maxUsernameLen)
	}
	for _, r := range folded {
		if r < 0x21 || r == 0x7f { // control chars, space family, DEL
			return fmt.Errorf("accounts: username must not contain whitespace or control characters")
		}
	}
	return nil
}

// validateParticipantID enforces the §A regex so the binding is a valid
// RFC 0016 identity by construction.
func validateParticipantID(id string) error {
	if !participantIDRegex.MatchString(id) {
		return fmt.Errorf("accounts: participant_id %q must match %s", id, participantIDRegex.String())
	}
	return nil
}
