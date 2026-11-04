package security

import "testing"

// TestDefaultRedactPatterns_IsTheRedactorsTable pins that the exported table
// cmd/genpatterns copies into Python is the one NewSecretRedactor applies:
// same names, same expressions, same order (RFC 0028 PR 3a).
func TestDefaultRedactPatterns_IsTheRedactorsTable(t *testing.T) {
	exported := DefaultRedactPatterns()
	installed := NewSecretRedactor().patterns
	if len(exported) != len(installed) {
		t.Fatalf("DefaultRedactPatterns has %d patterns, NewSecretRedactor %d",
			len(exported), len(installed))
	}
	for i, p := range exported {
		if p.Name != installed[i].name || p.Expr != installed[i].pattern.String() {
			t.Errorf("pattern %d: exported %q %q, installed %q %q",
				i, p.Name, p.Expr, installed[i].name, installed[i].pattern.String())
		}
	}
}

// TestDefaultRedactPatterns_ReturnsACopy pins that a caller editing the
// returned slice cannot change what a later redactor installs.
func TestDefaultRedactPatterns_ReturnsACopy(t *testing.T) {
	first := DefaultRedactPatterns()
	first[0].Expr = "changed"
	if DefaultRedactPatterns()[0].Expr == "changed" {
		t.Fatal("DefaultRedactPatterns returned shared state")
	}
}
