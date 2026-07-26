package accounts

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"fmt"
	"strings"

	"golang.org/x/crypto/argon2"
)

// Argon2id password hashing (RFC 0039 §C) over golang.org/x/crypto.
// Hashes are stored as PHC strings —
//
//	$argon2id$v=19$m=65536,t=3,p=4$<b64 salt>$<b64 key>
//
// — so the cost parameters ride inside the hash: a later config change
// never invalidates an existing credential, it only makes the next
// successful login rehash at current cost (verify-then-rehash, §C).

// Params are the Argon2id cost parameters, read from config by the
// caller (the `auth.password` block, §H). The zero value is invalid;
// use [DefaultParams] when no config applies.
type Params struct {
	MemoryKiB   uint32 // KiB of memory per hash
	Iterations  uint32 // time cost (passes)
	Parallelism uint8  // lanes
}

// DefaultParams mirrors the §H config defaults: 64 MiB, 3 passes,
// 4 lanes.
var DefaultParams = Params{MemoryKiB: 65536, Iterations: 3, Parallelism: 4}

const (
	// saltLen is the per-account random salt size (§C: 16 bytes from
	// crypto/rand).
	saltLen = 16
	// keyLen is the derived-key size. Fixed: verification compares
	// fixed-size derived keys, which is what makes it constant-time.
	keyLen = 32
	// argon2Version is pinned by the x/crypto implementation; encoded
	// and checked so a future algorithm rev is an explicit migration,
	// not a silent drift.
	argon2Version = argon2.Version
)

// Validate rejects parameter sets the KDF would refuse or degrade on.
func (p Params) Validate() error {
	if p.MemoryKiB == 0 || p.Iterations == 0 || p.Parallelism == 0 {
		return fmt.Errorf("accounts: argon2 params must be non-zero (got m=%d,t=%d,p=%d)",
			p.MemoryKiB, p.Iterations, p.Parallelism)
	}
	return nil
}

// HashPassword derives an Argon2id hash of `password` under `p` with a
// fresh 16-byte crypto/rand salt and returns the PHC string.
func HashPassword(password string, p Params) (string, error) {
	if err := p.Validate(); err != nil {
		return "", err
	}
	salt := make([]byte, saltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("accounts: read salt: %w", err)
	}
	key := argon2.IDKey([]byte(password), salt, p.Iterations, p.MemoryKiB, p.Parallelism, keyLen)
	return encodePHC(p, salt, key), nil
}

// VerifyPassword reports whether `password` matches the PHC-encoded
// hash. Derivation uses the parameters embedded in the hash (not the
// current config), and the comparison is crypto/subtle over fixed-size
// derived keys — constant-time by construction (§C).
func VerifyPassword(password, encoded string) (bool, error) {
	p, salt, key, err := decodePHC(encoded)
	if err != nil {
		return false, err
	}
	derived := argon2.IDKey([]byte(password), salt, p.Iterations, p.MemoryKiB, p.Parallelism, uint32(len(key)))
	return subtle.ConstantTimeCompare(derived, key) == 1, nil
}

// NeedsRehash reports whether the hash was produced under a parameter
// set other than `current` — the §C verify-then-rehash trigger. The
// caller re-stores via [HashPassword] + [Store.SetPasswordHash] only
// after a successful verification.
func NeedsRehash(encoded string, current Params) (bool, error) {
	p, _, key, err := decodePHC(encoded)
	if err != nil {
		return false, err
	}
	return p != current || len(key) != keyLen, nil
}

// dummySalt backs [DummyVerify]. Fixed — the point is spending the same
// KDF work as a real verification, not hiding a secret.
var dummySalt = [saltLen]byte{'p', 'e', 'r', 's', 'a', 't', 'r', 'i', 'x', '-', 'd', 'u', 'm', 'm', 'y', '0'}

// DummyVerify burns one full Argon2id verification against a fixed
// dummy salt and discards the result. The login path calls it when the
// supplied username matches no account, so a missing account and a
// wrong password are indistinguishable by response timing (§C
// account-existence non-disclosure) — and both return the identical
// 401 at the handler.
func DummyVerify(password string, p Params) {
	if p.Validate() != nil {
		p = DefaultParams
	}
	derived := argon2.IDKey([]byte(password), dummySalt[:], p.Iterations, p.MemoryKiB, p.Parallelism, keyLen)
	// Mirror the real path's comparison; always false against zeros
	// (an IDKey output is never all-zero for these inputs in practice,
	// and no caller reads the result anyway).
	subtle.ConstantTimeCompare(derived, make([]byte, keyLen))
}

// encodePHC renders the standard PHC string. Salt and key use raw
// (unpadded) standard base64 per the PHC spec.
func encodePHC(p Params, salt, key []byte) string {
	return fmt.Sprintf("$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2Version, p.MemoryKiB, p.Iterations, p.Parallelism,
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(key))
}

// decodePHC parses a PHC string produced by [encodePHC] (or any
// spec-conformant argon2id encoder), returning the embedded parameters,
// salt, and derived key.
func decodePHC(encoded string) (Params, []byte, []byte, error) {
	var zero Params
	parts := strings.Split(encoded, "$")
	// "" / "argon2id" / "v=19" / "m=..,t=..,p=.." / salt / key
	if len(parts) != 6 || parts[0] != "" {
		return zero, nil, nil, fmt.Errorf("accounts: malformed argon2 hash")
	}
	if parts[1] != "argon2id" {
		return zero, nil, nil, fmt.Errorf("accounts: unsupported hash algorithm %q", parts[1])
	}
	var version int
	if _, err := fmt.Sscanf(parts[2], "v=%d", &version); err != nil || version != argon2Version {
		return zero, nil, nil, fmt.Errorf("accounts: unsupported argon2 version %q", parts[2])
	}
	var p Params
	if _, err := fmt.Sscanf(parts[3], "m=%d,t=%d,p=%d", &p.MemoryKiB, &p.Iterations, &p.Parallelism); err != nil {
		return zero, nil, nil, fmt.Errorf("accounts: malformed argon2 parameters %q", parts[3])
	}
	if err := p.Validate(); err != nil {
		return zero, nil, nil, err
	}
	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil {
		return zero, nil, nil, fmt.Errorf("accounts: malformed argon2 salt: %w", err)
	}
	key, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil {
		return zero, nil, nil, fmt.Errorf("accounts: malformed argon2 key: %w", err)
	}
	if len(key) == 0 {
		return zero, nil, nil, fmt.Errorf("accounts: empty argon2 key")
	}
	return p, salt, key, nil
}
