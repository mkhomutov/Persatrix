package security

// RedactPattern is one entry of the secret redactor's default table: the
// marker name a match becomes (`[REDACTED:<Name>]`) and the RE2 expression.
type RedactPattern struct {
	Name string
	Expr string
}

// DefaultRedactPatterns returns a copy of the table [NewSecretRedactor]
// installs, in the order it applies them. cmd/genpatterns copies it into
// the agents' Python redactor (RFC 0028 PR 3a), so the decision log masks
// the secrets the audit log does.
func DefaultRedactPatterns() []RedactPattern {
	specs := defaultPatterns()
	out := make([]RedactPattern, len(specs))
	for i, s := range specs {
		out[i] = RedactPattern{Name: s.name, Expr: s.expr}
	}
	return out
}
