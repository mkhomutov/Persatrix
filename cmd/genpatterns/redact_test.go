package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mkhomutov/persatrix/internal/security"
)

// TestRedactSamples_CoverEveryPattern pins that the parity samples exercise
// every Go redact pattern, and that each sample is masked by the pattern it
// names, so a Python sample that passes proves that pattern matches.
func TestRedactSamples_CoverEveryPattern(t *testing.T) {
	r := security.NewSecretRedactor()
	covered := map[string]bool{}
	for _, s := range redactSamples {
		in := strings.Join(s.parts, "")
		got := r.Redact(in)
		if s.pattern == "" {
			if got != in {
				t.Errorf("clean sample %q was changed to %q", in, got)
			}
			continue
		}
		if !strings.Contains(got, "[REDACTED:"+s.pattern+"]") {
			t.Errorf("sample for %s: Redact(%q) = %q", s.pattern, in, got)
		}
		covered[s.pattern] = true
	}
	for _, p := range security.DefaultRedactPatterns() {
		if !covered[p.Name] {
			t.Errorf("no parity sample exercises %s", p.Name)
		}
	}
}

// TestRenderRedactPatterns_CopiesGoStringsVerbatim pins that each name and
// expression reaches the Python module unchanged and in order.
func TestRenderRedactPatterns_CopiesGoStringsVerbatim(t *testing.T) {
	src := renderRedactPatterns(security.DefaultRedactPatterns())
	at := 0
	for _, p := range security.DefaultRedactPatterns() {
		i := strings.Index(src[at:], pyRepr(p.Name)+", "+pyRepr(p.Expr))
		if i < 0 {
			t.Fatalf("%s is missing, changed or out of order in:\n%s", p.Name, src)
		}
		at += i
	}
	for n, line := range strings.Split(src, "\n") {
		if len(line) > 100 && !strings.HasSuffix(line, "# noqa: E501") {
			t.Errorf("line %d is %d characters, over ruff's 100: %q", n+1, len(line), line)
		}
	}
}

// TestRenderRedactSamples_KeepsGoOutputAndSplitsInput pins that the samples
// module carries what Go returned for each input, and never an input whole:
// GitHub's push-time secret scanner flags a full token shape even as test data.
func TestRenderRedactSamples_KeepsGoOutputAndSplitsInput(t *testing.T) {
	r := security.NewSecretRedactor()
	src := renderRedactSamples(r, redactSamples)
	for _, s := range redactSamples {
		in := strings.Join(s.parts, "")
		if !strings.Contains(src, pyRepr(r.Redact(in))) {
			t.Errorf("Go's output for %q is missing", in)
		}
		if s.pattern != "" && strings.Contains(src, pyRepr(in)) {
			t.Errorf("sample for %s is written whole: %q", s.pattern, in)
		}
	}
}

// TestCheck_FailsOnHandEdit pins the parity gate: fresh files pass, and a
// hand edit or a missing file fails and names the file.
func TestCheck_FailsOnHandEdit(t *testing.T) {
	dir := t.TempDir()
	outs := buildOutputs(outputPaths{
		patterns:      filepath.Join(dir, "security_patterns.py"),
		enums:         filepath.Join(dir, "security_enums.py"),
		redact:        filepath.Join(dir, "redact_patterns.py"),
		redactSamples: filepath.Join(dir, "_redact_parity_samples.py"),
	})
	if err := writeOutputs(outs, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	var report bytes.Buffer
	if !checkOutputs(outs, &report) {
		t.Fatalf("fresh files reported stale:\n%s", report.String())
	}

	edited := filepath.Join(dir, "redact_patterns.py")
	body, err := os.ReadFile(edited)
	if err != nil {
		t.Fatal(err)
	}
	body = bytes.Replace(body, []byte("{20,}"), []byte("{21,}"), 1)
	if err := os.WriteFile(edited, body, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "security_enums.py")); err != nil {
		t.Fatal(err)
	}
	report.Reset()
	if checkOutputs(outs, &report) {
		t.Fatal("a hand-edited file passed the check")
	}
	for _, name := range []string{"redact_patterns.py", "security_enums.py"} {
		if !strings.Contains(report.String(), name) {
			t.Errorf("report does not name %s:\n%s", name, report.String())
		}
	}
	if strings.Contains(report.String(), "security_patterns.py") {
		t.Errorf("report names an unchanged file:\n%s", report.String())
	}
}
