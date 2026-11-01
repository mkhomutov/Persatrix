// How agents prove who they are (RFC 0009 Phase 4): the agent token issuer,
// the fleet enrollment secret and the unquarantine secret, handed to the
// REST server together. Kept out of main.go, which is at its size cap. The
// blank line below keeps this out of the package comment, which is in
// main.go.

package main

import (
	"os"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/server"
)

// enrollmentSecretEnvVar names the fleet enrollment secret: the shared
// secret agents show when they register (RFC 0009 open question 9). It is
// set the way SECURITY_UNQUARANTINE_TOKEN is.
const enrollmentSecretEnvVar = "SECURITY_ENROLLMENT_SECRET"

// agentIdentityOptions returns the REST server's options for agent
// identity: tokens, the issuer that signs the tokens registration answers
// with, always; the unquarantine and enrollment secrets when each is set.
func agentIdentityOptions(logger *zap.Logger, auditor security.AuditLogger,
	tokens *security.AgentTokenIssuer,
) []server.ServerOption {
	opts := []server.ServerOption{server.WithAgentTokenIssuer(tokens)}
	// PR #244 H-02 — when unset, unquarantineToken writes a startup WARN and
	// an `unquarantine.endpoint.open` audit event (M-05); nil auditor safe.
	if tok := unquarantineToken(logger, auditor); tok != "" {
		opts = append(opts, server.WithUnquarantineToken(tok))
	}
	if secret := enrollmentSecret(logger); secret != "" {
		opts = append(opts, server.WithEnrollmentSecret(secret))
	}
	return opts
}

// enrollmentSecret reads the fleet enrollment secret from the environment,
// "" when it is unset, and logs which, never the secret. Unset, registration
// admits any caller, as before: PR 9 of the Phases 3–4 plan refuses every
// registration under auth.mode: enabled until it is set, and warns then.
func enrollmentSecret(logger *zap.Logger) string {
	secret := os.Getenv(enrollmentSecretEnvVar)
	if secret == "" {
		logger.Info("agent enrollment secret not set",
			zap.String("source", enrollmentSecretEnvVar),
			zap.String("posture", "registration admits any caller"))
		return ""
	}
	logger.Info("agent enrollment secret set",
		zap.String("source", enrollmentSecretEnvVar),
		zap.String("posture", "registration refuses a wrong secret"))
	return secret
}
