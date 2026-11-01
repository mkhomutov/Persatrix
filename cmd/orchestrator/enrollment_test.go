package main

import (
	"fmt"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/security"
)

// RFC 0009 Phases 3–4 plan PR 6: the fleet enrollment secret is read from
// SECURITY_ENROLLMENT_SECRET at startup, and the log says whether it is set
// without ever printing it.

func TestEnrollmentSecret_SetIsReturnedAndNeverLogged(t *testing.T) {
	t.Setenv(enrollmentSecretEnvVar, "fleet-s3cret")
	core, recorded := observer.New(zap.DebugLevel)

	assert.Equal(t, "fleet-s3cret", enrollmentSecret(zap.New(core)))
	entries := recorded.FilterMessage("agent enrollment secret set").All()
	require.Len(t, entries, 1)
	assert.Equal(t, zap.InfoLevel, entries[0].Level)
	assert.NotContains(t, fmt.Sprint(recorded.All()), "fleet-s3cret",
		"the secret appears in no log line")
}

func TestEnrollmentSecret_UnsetSaysRegistrationTakesAnyCaller(t *testing.T) {
	t.Setenv(enrollmentSecretEnvVar, "")
	core, recorded := observer.New(zap.DebugLevel)

	assert.Equal(t, "", enrollmentSecret(zap.New(core)))
	entries := recorded.FilterMessage("agent enrollment secret not set").All()
	require.Len(t, entries, 1)
	assert.Equal(t, enrollmentSecretEnvVar, entries[0].ContextMap()["source"])
}

func TestAgentIdentityOptions_OnePerSettingThatIsSet(t *testing.T) {
	tokens := security.NewAgentTokenIssuer()

	t.Setenv("SECURITY_UNQUARANTINE_TOKEN", "")
	t.Setenv(enrollmentSecretEnvVar, "")
	assert.Len(t, agentIdentityOptions(zap.NewNop(), nil, tokens), 1,
		"the token issuer is always wired")

	t.Setenv("SECURITY_UNQUARANTINE_TOKEN", "op-s3cret")
	t.Setenv(enrollmentSecretEnvVar, "fleet-s3cret")
	assert.Len(t, agentIdentityOptions(zap.NewNop(), nil, tokens), 3)
}
