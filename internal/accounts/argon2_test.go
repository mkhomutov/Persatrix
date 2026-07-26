package accounts

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// testParams keeps the KDF cheap for unit tests; DefaultParams costs
// ~64 MiB × 3 passes per call, which is the production point, not a
// test-loop one.
var testParams = Params{MemoryKiB: 1024, Iterations: 1, Parallelism: 1}

func TestHashPassword_VerifyRoundTrip(t *testing.T) {
	hash, err := HashPassword("correct horse battery staple", testParams)
	require.NoError(t, err)

	ok, err := VerifyPassword("correct horse battery staple", hash)
	require.NoError(t, err)
	assert.True(t, ok)

	ok, err = VerifyPassword("wrong password", hash)
	require.NoError(t, err)
	assert.False(t, ok)
}

func TestHashPassword_DefaultParams_ProducesSpecPHCString(t *testing.T) {
	hash, err := HashPassword("s3cret", DefaultParams)
	require.NoError(t, err)
	assert.True(t, strings.HasPrefix(hash, "$argon2id$v=19$m=65536,t=3,p=4$"),
		"§H default cost parameters must ride inside the PHC string, got %q", hash)

	ok, err := VerifyPassword("s3cret", hash)
	require.NoError(t, err)
	assert.True(t, ok)
}

func TestHashPassword_SaltIsPerHash(t *testing.T) {
	h1, err := HashPassword("same password", testParams)
	require.NoError(t, err)
	h2, err := HashPassword("same password", testParams)
	require.NoError(t, err)
	assert.NotEqual(t, h1, h2, "each hash must draw a fresh random salt (§C)")

	for _, h := range []string{h1, h2} {
		ok, err := VerifyPassword("same password", h)
		require.NoError(t, err)
		assert.True(t, ok)
	}
}

func TestVerifyPassword_UsesEmbeddedParamsNotCurrentConfig(t *testing.T) {
	// A hash minted under one parameter set must keep verifying after
	// the config moves — the §C no-invalidation contract. Verification
	// takes no Params at all; this pins that the embedded set suffices.
	hash, err := HashPassword("pw", Params{MemoryKiB: 2048, Iterations: 2, Parallelism: 2})
	require.NoError(t, err)

	ok, err := VerifyPassword("pw", hash)
	require.NoError(t, err)
	assert.True(t, ok)
}

func TestNeedsRehash(t *testing.T) {
	hash, err := HashPassword("pw", testParams)
	require.NoError(t, err)

	same, err := NeedsRehash(hash, testParams)
	require.NoError(t, err)
	assert.False(t, same, "a hash at current cost must not trigger a rehash")

	changed, err := NeedsRehash(hash, Params{MemoryKiB: 2048, Iterations: 1, Parallelism: 1})
	require.NoError(t, err)
	assert.True(t, changed, "an out-of-date parameter set must trigger verify-then-rehash (§C)")
}

func TestVerifyPassword_MalformedHashes(t *testing.T) {
	for name, encoded := range map[string]string{
		"empty":            "",
		"not a phc string": "plainly-not-a-hash",
		"wrong algorithm":  "$argon2i$v=19$m=1024,t=1,p=1$c2FsdHNhbHRzYWx0c2FsdA$AAAA",
		"wrong version":    "$argon2id$v=18$m=1024,t=1,p=1$c2FsdHNhbHRzYWx0c2FsdA$AAAA",
		"zero params":      "$argon2id$v=19$m=0,t=0,p=0$c2FsdHNhbHRzYWx0c2FsdA$AAAA",
		"bad salt base64":  "$argon2id$v=19$m=1024,t=1,p=1$!!!$AAAA",
		"bad key base64":   "$argon2id$v=19$m=1024,t=1,p=1$c2FsdHNhbHRzYWx0c2FsdA$!!!",
		"empty key":        "$argon2id$v=19$m=1024,t=1,p=1$c2FsdHNhbHRzYWx0c2FsdA$",
	} {
		t.Run(name, func(t *testing.T) {
			ok, err := VerifyPassword("pw", encoded)
			assert.Error(t, err)
			assert.False(t, ok)
		})
	}
}

func TestParams_Validate(t *testing.T) {
	assert.NoError(t, DefaultParams.Validate())
	assert.NoError(t, testParams.Validate())
	assert.Error(t, Params{}.Validate())
	assert.Error(t, Params{MemoryKiB: 1024, Iterations: 1}.Validate(), "zero parallelism")
	assert.Error(t, Params{MemoryKiB: 1024, Parallelism: 1}.Validate(), "zero iterations")

	_, err := HashPassword("pw", Params{})
	assert.Error(t, err, "HashPassword must refuse invalid params rather than degrade")
}

func TestDummyVerify_BurnsAKDFWithoutPanicking(t *testing.T) {
	// The absent-username login path calls this to stay timing-
	// indistinguishable from a real verification (§C). Smoke both the
	// configured-params path and the invalid-params fallback.
	DummyVerify("any password", testParams)
	DummyVerify("any password", Params{})
}
