package main

import (
	"regexp"
	"strings"
	"testing"

	"github.com/mkhomutov/persatrix/internal/security"
)

// TestPythonRegex_WritesGosReading pins each rewrite: Go's space set for
// `\s`, `\Z` for `$`, Python's ASCII mode, and under `(?i)` the two
// non-ASCII letters Go folds with s and k.
func TestPythonRegex_WritesGosReading(t *testing.T) {
	cases := []struct{ in, want string }{
		{`a\sb`, `(?a)a[\t\n\f\r ]b`},
		{`[^\s,]+`, `(?a)[^\t\n\f\r ,]+`},
		{`x[\s\S]*?(y|$)`, `(?a)x[\s\S]*?(y|\Z)`},
		{`(?i)token`, "(?ai)to[k\u212a]en"},
		{`(?i)[A-Z0-9_\-]`, "(?ai)[A-Z0-9_\\-\u017f\u212a]"},
		{`(?i)[ab]x`, `(?ai)[ab]x`},
		{`(?i)[^s]`, "(?ai)[^s\u017f]"},
		{`[a-z]{20,}`, `(?a)[a-z]{20,}`},
		{`[ _-]?key["']?[:=\s]+`, `(?a)[ _-]?key["']?[:=\t\n\f\r ]+`},
		// Go's spaces end in a literal space, so a '-' after them is
		// escaped: Python would read ` -a` as a range from space to a.
		{`[\s-a]`, `(?a)[\t\n\f\r \-a]`},
		{`[\s--/]`, `(?a)[\t\n\f\r \--/]`},
		// Go reads `{,n}` as text; Python would read it as 0..n repeats.
		{`x{,5}`, `(?a)x\{,5}`},
		{`x{,}`, `(?a)x\{,}`},
	}
	for _, tc := range cases {
		got, err := pythonRegex(tc.in)
		if err != nil || got != tc.want {
			t.Errorf("pythonRegex(%q) = %q, %v; want %q", tc.in, got, err, tc.want)
		}
	}
}

// TestPythonRegex_RefusesWhatItDoesNotRewrite pins that a construct the two
// engines read differently, and that the rewrite does not cover, fails the
// generator rather than reaching Python unchanged.
func TestPythonRegex_RefusesWhatItDoesNotRewrite(t *testing.T) {
	for _, in := range []string{
		`\bx`, `\w+`, `\d`, `a\S`, `[\S]`, `[\w]`, `(?m)a$`, `a(?i)b`, `(?:a)`,
		`[[:alpha:]]`, `\pL`, `x\`, `[abc`, "caf\u00e9",
	} {
		if got, err := pythonRegex(in); err == nil {
			t.Errorf("pythonRegex(%q) = %q; want an error", in, got)
		}
	}
}

// TestPythonRegex_TranslatesEveryDefaultPattern pins that the redactor's
// whole table goes through.
func TestPythonRegex_TranslatesEveryDefaultPattern(t *testing.T) {
	for _, p := range security.DefaultRedactPatterns() {
		if _, err := pythonRegex(p.Expr); err != nil {
			t.Errorf("%s: %v", p.Name, err)
		}
	}
}

// TestRedactSamples_SplitHidesTheirToken pins that no part of a sample is
// matched on its own by the pattern the sample exercises, so no whole token
// shape reaches either source file.
func TestRedactSamples_SplitHidesTheirToken(t *testing.T) {
	exprs := map[string]*regexp.Regexp{}
	for _, p := range security.DefaultRedactPatterns() {
		exprs[p.Name] = regexp.MustCompile(p.Expr)
	}
	for _, s := range redactSamples {
		if s.pattern == "" {
			continue
		}
		if len(s.parts) < 2 {
			t.Errorf("sample for %s is not split: %q", s.pattern, s.parts)
		}
		for _, part := range s.parts {
			if exprs[s.pattern].MatchString(part) {
				t.Errorf("part %q of a %s sample is a whole token", part, s.pattern)
			}
		}
	}
}

// TestPyStr_EscapesWhatIsNotPrintableASCII pins that a control character
// or a non-ASCII letter in a sample is written as an escape.
func TestPyStr_EscapesWhatIsNotPrintableASCII(t *testing.T) {
	got := pyStr("a\x0b\u00a0\u0131\"\\\n\U0001F600")
	want := `"a\x0b\xa0\u0131\"\\\n\U0001f600"`
	if got != want {
		t.Errorf("pyStr = %s; want %s", got, want)
	}
	if strings.ContainsFunc(pyStr("\u212a"), func(r rune) bool { return r > 0x7e }) {
		t.Error("pyStr left a non-ASCII rune")
	}
}
