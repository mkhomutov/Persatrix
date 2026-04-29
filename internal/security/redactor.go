package security

import (
	"fmt"
	"reflect"
	"regexp"
)

// Redactor scrubs known secret patterns from strings and arbitrary structs
// before they are written to any sink (audit log, agent task results,
// structured log records).
//
// The default constructor [NewSecretRedactor] installs the five patterns
// from RFC 0009 §I. Callers may compose additional patterns via
// [Redactor.AddPattern].
//
// Redactor is safe for concurrent use; the internal pattern slice is only
// mutated through [Redactor.AddPattern] which is documented as not safe to
// call once redaction is in flight.
type Redactor interface {
	Redact(s string) string
	RedactStruct(v any) any
}

// SecretRedactor implements [Redactor] with a list of compiled regex patterns.
type SecretRedactor struct {
	patterns []redactPattern
}

type redactPattern struct {
	name    string
	pattern *regexp.Regexp
	replace string
}

// reflectionDepthCap bounds [SecretRedactor.RedactStruct] recursion to avoid
// stack overflow on adversarial / pathological inputs (PR #232 review SF-2).
//
// A depth of 32 comfortably exceeds any realistic AuditEvent.Detail nesting
// while still terminating on a deeply linked-list-style fixture.
const reflectionDepthCap = 32

// redactDepthExceededMarker replaces fields whose redaction would exceed
// [reflectionDepthCap]. The form mirrors the per-pattern replace strings so
// downstream consumers can use a single regex to detect any redacted value.
const redactDepthExceededMarker = "[REDACTED:max-depth-exceeded]"

// NewSecretRedactor returns a [SecretRedactor] preloaded with the five
// default patterns from RFC 0009 §I:
//
//   - anthropic-api-key
//   - openai-api-key
//   - bearer-token
//   - aws-access-key
//   - generic-secret
//
// Pattern compilation happens once at construction; a malformed default
// pattern would be a programmer error and panics here rather than failing
// silently at first redact-call.
func NewSecretRedactor() *SecretRedactor {
	r := &SecretRedactor{}
	for _, p := range defaultPatterns() {
		if err := r.AddPattern(p.name, p.expr); err != nil {
			panic(fmt.Sprintf("security: malformed default redact pattern %q: %v", p.name, err))
		}
	}
	return r
}

type patternSpec struct {
	name string
	expr string
}

func defaultPatterns() []patternSpec {
	return []patternSpec{
		// Order matters only for debugging; matches do not overlap in practice.
		{name: "anthropic-api-key", expr: `sk-ant-[A-Za-z0-9\-_]{20,}`},
		{name: "openai-api-key", expr: `sk-[A-Za-z0-9]{20,}`},
		{name: "bearer-token", expr: `(?i)bearer\s+[A-Za-z0-9\-_.~+/]+=*`},
		{name: "aws-access-key", expr: `AKIA[0-9A-Z]{16}`},
		{name: "generic-secret", expr: `(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S+`},
	}
}

// AddPattern compiles expr and appends it to the redactor's pattern list.
// Returns an error if the regex fails to compile.
//
// Not safe to call concurrently with [SecretRedactor.Redact] /
// [SecretRedactor.RedactStruct]; intended to run during process startup.
func (r *SecretRedactor) AddPattern(name, expr string) error {
	re, err := regexp.Compile(expr)
	if err != nil {
		return fmt.Errorf("security: compile redact pattern %q: %w", name, err)
	}
	r.patterns = append(r.patterns, redactPattern{
		name:    name,
		pattern: re,
		replace: "[REDACTED:" + name + "]",
	})
	return nil
}

// Redact returns s with every match of every registered pattern replaced by
// the corresponding `[REDACTED:<name>]` marker.
//
// Patterns are applied in registration order. Earlier matches do not feed
// into later patterns (the marker text contains no secret-like content).
func (r *SecretRedactor) Redact(s string) string {
	for _, p := range r.patterns {
		s = p.pattern.ReplaceAllString(s, p.replace)
	}
	return s
}

// RedactStruct walks v reflectively and returns a copy with every reachable
// string field redacted via [SecretRedactor.Redact].
//
// Supported field shapes:
//   - string, []string
//   - map[string]string (values redacted; keys preserved — keys-as-secrets
//     would be a misconfiguration, not a leak vector)
//   - nested structs and pointer-to-struct
//
// Skipped: time.Time, numeric types, unexported fields, channel/func/unsafe.
//
// Cycle and depth bound (PR #232 review SF-2): visited pointer addresses
// are tracked in a per-call set; recursion is capped at
// [reflectionDepthCap] levels. When the cap is hit or a previously visited
// pointer is re-entered, the current node is replaced with
// [redactDepthExceededMarker] rather than panicking on stack overflow.
//
// The input is not mutated. The returned value is a deep copy of every
// container the walk descends into; primitives are returned by value.
func (r *SecretRedactor) RedactStruct(v any) any {
	if v == nil {
		return nil
	}
	visited := make(map[uintptr]struct{})
	out := r.walk(reflect.ValueOf(v), 0, visited)
	if !out.IsValid() {
		return nil
	}
	return out.Interface()
}

func (r *SecretRedactor) walk(v reflect.Value, depth int, visited map[uintptr]struct{}) reflect.Value {
	if !v.IsValid() {
		return v
	}
	if depth > reflectionDepthCap {
		return reflect.ValueOf(redactDepthExceededMarker)
	}

	switch v.Kind() {
	case reflect.Interface:
		if v.IsNil() {
			return v
		}
		inner := r.walk(v.Elem(), depth+1, visited)
		// Re-wrap into an interface{} so callers see the original shape.
		out := reflect.New(v.Type()).Elem()
		if inner.IsValid() {
			out.Set(inner)
		}
		return out

	case reflect.Pointer:
		if v.IsNil() {
			return v
		}
		addr := v.Pointer()
		if _, seen := visited[addr]; seen {
			return reflect.ValueOf(redactDepthExceededMarker)
		}
		visited[addr] = struct{}{}
		defer delete(visited, addr)
		inner := r.walk(v.Elem(), depth+1, visited)
		if !inner.IsValid() {
			return v
		}
		// If the recursive call replaced the value with the marker string we
		// cannot fit it back into the original pointer's type — return the
		// marker directly. Callers that need shape-preservation can wrap.
		if inner.Type() == reflect.TypeOf(redactDepthExceededMarker) && v.Elem().Type().Kind() != reflect.String {
			return inner
		}
		out := reflect.New(v.Elem().Type())
		if inner.Type().AssignableTo(v.Elem().Type()) {
			out.Elem().Set(inner)
		} else {
			out.Elem().Set(v.Elem())
		}
		return out

	case reflect.Struct:
		// Skip well-known opaque structs.
		if isOpaqueStruct(v.Type()) {
			return v
		}
		out := reflect.New(v.Type()).Elem()
		for i := 0; i < v.NumField(); i++ {
			f := v.Type().Field(i)
			if !f.IsExported() {
				continue
			}
			fieldOut := r.walk(v.Field(i), depth+1, visited)
			if fieldOut.IsValid() && fieldOut.Type().AssignableTo(out.Field(i).Type()) {
				out.Field(i).Set(fieldOut)
			} else {
				out.Field(i).Set(v.Field(i))
			}
		}
		return out

	case reflect.Slice:
		if v.IsNil() {
			return v
		}
		out := reflect.MakeSlice(v.Type(), v.Len(), v.Len())
		for i := 0; i < v.Len(); i++ {
			elemOut := r.walk(v.Index(i), depth+1, visited)
			if elemOut.IsValid() && elemOut.Type().AssignableTo(out.Index(i).Type()) {
				out.Index(i).Set(elemOut)
			} else {
				out.Index(i).Set(v.Index(i))
			}
		}
		return out

	case reflect.Array:
		out := reflect.New(v.Type()).Elem()
		for i := 0; i < v.Len(); i++ {
			elemOut := r.walk(v.Index(i), depth+1, visited)
			if elemOut.IsValid() && elemOut.Type().AssignableTo(out.Index(i).Type()) {
				out.Index(i).Set(elemOut)
			} else {
				out.Index(i).Set(v.Index(i))
			}
		}
		return out

	case reflect.Map:
		if v.IsNil() {
			return v
		}
		out := reflect.MakeMapWithSize(v.Type(), v.Len())
		iter := v.MapRange()
		for iter.Next() {
			valOut := r.walk(iter.Value(), depth+1, visited)
			if valOut.IsValid() && valOut.Type().AssignableTo(out.Type().Elem()) {
				out.SetMapIndex(iter.Key(), valOut)
			} else {
				out.SetMapIndex(iter.Key(), iter.Value())
			}
		}
		return out

	case reflect.String:
		return reflect.ValueOf(r.Redact(v.String()))

	default:
		// Numbers, booleans, channels, funcs — return as-is.
		return v
	}
}

// isOpaqueStruct lists struct types whose internals must not be mutated by
// reflective redaction. time.Time is the canonical case — its unexported
// fields encode wall/mono clock state and walking them would corrupt the value.
func isOpaqueStruct(t reflect.Type) bool {
	pkg := t.PkgPath()
	name := t.Name()
	switch {
	case pkg == "time" && name == "Time":
		return true
	}
	return false
}
