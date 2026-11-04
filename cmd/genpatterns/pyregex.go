package main

import (
	"fmt"
	"strings"
)

// Python's `re` reads three pieces of the secret redactor's patterns
// differently from Go's RE2, and each difference can leak a secret: a
// pattern that takes more text in Python swallows the start of the secret a
// later pattern masks (ISSUE-0154). pythonRegex writes a Go pattern so Python
// matches what Go matches:
//
//   - `\s` is written out as Go's five ASCII spaces. Python's `\s` also takes
//     `\v` and, outside ASCII mode, every Unicode space.
//   - `$` becomes `\Z`. Python's `$` also matches before a final newline.
//   - The pattern runs in Python's ASCII mode, where `(?i)` folds only ASCII
//     letters. Go also folds the long s (U+017F) with s and the Kelvin sign
//     (U+212A) with k, so under `(?i)` those two are added wherever s or k
//     can match.
//   - `{,` becomes `\{,`. Go reads `{,n}` as text; Python reads it as 0 to n
//     repeats, which can match nothing at all.
//   - Inside a class, a `-` right after Go's spaces is escaped: they end in
//     a literal space, and Python would read ` -x` as a range from space to x.
//
// Anything else the two engines may read differently is an error (`\b`,
// `\w`, `\d`, any other escaped letter, flags past a leading `(?i)`, a `[`
// inside a class, non-ASCII text), so a new Go pattern that uses one fails the
// generator instead of reaching Python with another meaning.

// goSpaces is Go's `\s`, as escapes Python reads the same way.
const goSpaces = `\t\n\f\r `

// goFolds are the non-ASCII letters Go's `(?i)` folds with an ASCII one.
var goFolds = []struct {
	lower, upper byte
	extra        rune
}{
	{'s', 'S', '\u017f'},
	{'k', 'K', '\u212a'},
}

func pythonRegex(expr string) (string, error) {
	body, fold := strings.CutPrefix(expr, "(?i)")
	var b strings.Builder
	if fold {
		b.WriteString("(?ai)")
	} else {
		b.WriteString("(?a)")
	}
	for i := 0; i < len(body); {
		c := body[i]
		switch {
		case c >= 0x80:
			return "", fmt.Errorf("%q: non-ASCII text at %d", expr, i)
		case c == '\\':
			if i+1 == len(body) {
				return "", fmt.Errorf("%q: trailing backslash", expr)
			}
			switch e := body[i+1]; {
			case e == 's':
				b.WriteString("[" + goSpaces + "]")
			case isASCIIPunct(e):
				b.WriteString(body[i : i+2])
			default:
				return "", fmt.Errorf(`%q: \%c is not rewritten`, expr, e)
			}
			i += 2
		case c == '[':
			class, n, err := pythonClass(body[i:], fold)
			if err != nil {
				return "", fmt.Errorf("%q: %w", expr, err)
			}
			b.WriteString(class)
			i += n
		case strings.HasPrefix(body[i:], "(?"):
			return "", fmt.Errorf("%q: flags at %d are not rewritten", expr, i)
		case c == '$':
			b.WriteString(`\Z`)
			i++
		case strings.HasPrefix(body[i:], "{,"):
			b.WriteString(`\{`)
			i++
		default:
			if extra, ok := foldExtra(c, fold); ok {
				b.WriteString("[" + string(c) + string(extra) + "]")
			} else {
				b.WriteByte(c)
			}
			i++
		}
	}
	return b.String(), nil
}

// pythonClass rewrites the character class that s starts with and returns
// it with the number of bytes of s it took.
func pythonClass(s string, fold bool) (string, int, error) {
	if strings.HasPrefix(s, `[\s\S]`) {
		// Any character, in both engines.
		return `[\s\S]`, len(`[\s\S]`), nil
	}
	i := 1
	head := "["
	if i < len(s) && s[i] == '^' {
		head, i = "[^", i+1
	}
	var items []string
	var spans [][2]byte
	for first := true; ; first = false {
		if i == len(s) {
			return "", 0, fmt.Errorf("unclosed class")
		}
		if s[i] == ']' && !first {
			break
		}
		lo, text, err := classAtom(s[i:])
		if err != nil {
			return "", 0, err
		}
		i += len(text)
		hi := lo
		if text != `\s` && i+1 < len(s) && s[i] == '-' && s[i+1] != ']' {
			end, endText, err := classAtom(s[i+1:])
			if err != nil || endText == `\s` {
				return "", 0, fmt.Errorf("bad range at %q", s[:i+1])
			}
			text += "-" + endText
			hi = end
			i += 1 + len(endText)
		}
		if text == `\s` {
			items = append(items, goSpaces)
		} else {
			if len(items) > 0 && items[len(items)-1] == goSpaces && text[0] == '-' {
				// The spaces end in a literal space; ` -` would start a range.
				text = `\` + text
			}
			items = append(items, text)
			spans = append(spans, [2]byte{lo, hi})
		}
	}
	var extras string
	for _, f := range goFolds {
		if fold && (covers(spans, f.lower) || covers(spans, f.upper)) {
			extras += string(f.extra)
		}
	}
	if extras != "" && items[len(items)-1] == "-" {
		// A literal '-' last would make a range of the letters added after it.
		items[len(items)-1] = `\-`
	}
	return head + strings.Join(items, "") + extras + "]", i + 1, nil
}

// classAtom reads one item of a class: a character, an escaped punctuation
// character, or `\s`. It returns the character (0 for `\s`) and the text it
// took.
func classAtom(s string) (byte, string, error) {
	switch c := s[0]; {
	case c >= 0x80:
		return 0, "", fmt.Errorf("non-ASCII text in a class")
	case c == '[':
		return 0, "", fmt.Errorf("a [ inside a class is not rewritten")
	case c != '\\':
		return c, s[:1], nil
	case len(s) < 2:
		return 0, "", fmt.Errorf("trailing backslash")
	case s[1] == 's':
		return 0, s[:2], nil
	case isASCIIPunct(s[1]):
		return s[1], s[:2], nil
	default:
		return 0, "", fmt.Errorf(`\%c in a class is not rewritten`, s[1])
	}
}

func covers(spans [][2]byte, c byte) bool {
	for _, sp := range spans {
		if sp[0] <= c && c <= sp[1] {
			return true
		}
	}
	return false
}

// foldExtra returns the non-ASCII letter Go folds with c under `(?i)`.
func foldExtra(c byte, fold bool) (rune, bool) {
	for _, f := range goFolds {
		if fold && (c == f.lower || c == f.upper) {
			return f.extra, true
		}
	}
	return 0, false
}

func isASCIIPunct(c byte) bool {
	return c > ' ' && c < 0x7f && !isASCIIAlnum(c)
}

func isASCIIAlnum(c byte) bool {
	return '0' <= c && c <= '9' || 'a' <= c && c <= 'z' || 'A' <= c && c <= 'Z'
}

// pyStr writes s as a Python string literal in printable ASCII: a control
// character or a non-ASCII letter in a pattern or a sample is written as an
// escape, so a reviewer sees it. It is never a raw string: a backslash is
// written doubled, so Python reads back Go's text unchanged. Every generated
// file uses it.
func pyStr(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '\\':
			b.WriteString(`\\`)
		case r == '"':
			b.WriteString(`\"`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r >= ' ' && r < 0x7f:
			b.WriteRune(r)
		case r < 0x100:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r < 0x10000:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteByte('"')
	return b.String()
}
