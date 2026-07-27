package accounts

import (
	"context"
	"errors"
	"fmt"
)

// Authenticator is the §I extension seam: every authentication method —
// password today, OIDC/SAML/API-key later — reduces credentials to an
// account, and every login path then ends the same way, in
// [Store.IssueSession]. The session layer never learns which method ran.
type Authenticator interface {
	// Authenticate verifies `creds` and returns the account ID they
	// prove. Failures that a caller must not be able to tell apart
	// (unknown username, wrong password, non-password account) are all
	// ErrInvalidCredentials; a correct credential on a disabled account
	// is ErrAccountDisabled.
	Authenticate(ctx context.Context, creds Credentials) (accountID string, err error)
}

// Credentials carries what a login attempt presented. The password
// method reads Username + Password; a future method extends this
// struct rather than the interface.
type Credentials struct {
	Username string
	Password string
}

// ErrInvalidCredentials — the §C single failure sentinel: unknown
// username and wrong password are deliberately the same error (and the
// same KDF cost — see DummyVerify), so response content and timing
// disclose nothing about account existence.
var ErrInvalidCredentials = errors.New("accounts: invalid credentials")

// passwordAuthenticator implements Authenticator over the account
// store's argon2id credentials (§C).
type passwordAuthenticator struct {
	store  *Store
	params Params // current cost — the §C verify-then-rehash target
}

// NewPasswordAuthenticator builds the password-method Authenticator.
// `params` is the current Argon2id cost from config (§H); stored hashes
// verify under their own embedded parameters and are re-stored at
// `params` on the next successful login.
func NewPasswordAuthenticator(store *Store, params Params) Authenticator {
	return &passwordAuthenticator{store: store, params: params}
}

func (a *passwordAuthenticator) Authenticate(ctx context.Context, creds Credentials) (string, error) {
	acct, err := a.store.GetAccountByUsername(ctx, creds.Username)
	if errors.Is(err, ErrAccountNotFound) {
		// Burn the same KDF work a real verification costs, so a missing
		// account and a wrong password are indistinguishable by timing
		// (§C). Residual: the dummy burns *current* config cost while a
		// real verification burns the *hash-embedded* cost, so during a
		// cost-raise drift window (stale hashes rehash only on the next
		// successful login) the two can differ; the window closes as
		// logins rehash.
		DummyVerify(creds.Password, a.params)
		return "", ErrInvalidCredentials
	}
	if err != nil {
		return "", err
	}
	if acct.AuthMethod != AuthMethodPassword || acct.PasswordHash == "" {
		// An account that cannot password-login fails exactly like a bad
		// password — its method is not for this caller to probe.
		DummyVerify(creds.Password, a.params)
		return "", ErrInvalidCredentials
	}

	ok, err := VerifyPassword(creds.Password, acct.PasswordHash)
	if err != nil {
		// A malformed stored hash is store corruption — loud, not a 401.
		return "", fmt.Errorf("accounts: verify credential for account %s: %w", acct.ID, err)
	}
	if !ok {
		return "", ErrInvalidCredentials
	}
	// Status is checked only after the credential proves out: a caller
	// without the password learns nothing about the account's state, and
	// the KDF cost is identical on every path.
	if acct.Status != StatusActive {
		return "", ErrAccountDisabled
	}

	if stale, err := NeedsRehash(acct.PasswordHash, a.params); err == nil && stale {
		// §C verify-then-rehash: opportunistically re-store at current
		// cost. Best-effort — a rehash failure must not fail a login the
		// credential already passed; the next login retries.
		if rehashed, err := HashPassword(creds.Password, a.params); err == nil {
			_ = a.store.SetPasswordHash(ctx, acct.ID, rehashed)
		}
	}
	return acct.ID, nil
}
