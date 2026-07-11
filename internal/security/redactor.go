package security

import (
	"fmt"
	"reflect"
	"regexp"
	"sync"
)

// Redactor scrubs known secret patterns from strings and arbitrary structs
// before they are written to any sink (audit log, agent task results,
// structured log records).
//
// The default constructor [NewSecretRedactor] installs the five patterns
// from RFC 0009 §I. Callers may compose additional patterns via
// [Redactor.AddPattern].
//
// Implementations must be safe for concurrent use by multiple goroutines.
type Redactor interface {
	Redact(s string) string
	RedactStruct(v any) any
}

// SecretRedactor implements [Redactor] with a list of compiled regex
// patterns. The pattern slice is guarded by an RWMutex so concurrent
// readers ([Redact] / [RedactStruct]) and writers ([AddPattern]) cannot
// race — PR #233 review flagged the prior "safe for concurrent use" doc
// claim as enforced only by caller convention. AddPattern is still
// expected to be a startup-time call in practice; the lock simply makes
// the contract self-enforcing rather than documentation-only.
type SecretRedactor struct {
	mu       sync.RWMutex
	patterns []redactPattern
}

type redactPattern struct {
	name    string
	pattern *regexp.Regexp
	replace string
}

// Depth-cap helpers (reflectionDepthCap, redactDepthExceededMarker, the
// typed [depthMarker] sentinel, [isDepthMarker], and [newDepthMarker])
// live in redactor_depth.go to keep this file under the 500-line cap.

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
		// `anthropic-api-key` runs before `openai-api-key` so the more specific
		// `sk-ant-…` prefix wins on Anthropic keys (covered by
		// `TestRedact_PatternOrdering`).
		{name: "anthropic-api-key", expr: `sk-ant-[A-Za-z0-9\-_]{20,}`},
		// PR #233 review Nice-to-have #2: Stripe live secret/publishable
		// keys carry a `sk_live_` / `pk_live_` prefix. Run before the
		// `openai-api-key` pattern so `sk_live_…` is captured by the
		// Stripe pattern (which uses `_`, not `-`, after `sk`/`pk`); the
		// OpenAI pattern uses `sk-` so they are disjoint, but ordering
		// is documented for grep-clarity.
		{name: "stripe-key", expr: `[sp]k_live_[A-Za-z0-9]{20,}`},
		// PR #233 review MF-1: real-world OpenAI keys (e.g. `sk-proj-AbCd_…`)
		// embed `-` and `_` in the suffix. The pre-fix `[A-Za-z0-9]{20,}` class
		// terminated at the first `-`, leaving the rest of the secret in plain
		// text. Allow `-` and `_` in the suffix charset so the full key is
		// captured and replaced.
		{name: "openai-api-key", expr: `sk-[A-Za-z0-9_\-]{20,}`},
		// PR #233 review Nice-to-have #2: GitHub fine-grained
		// (`github_pat_…`) and classic (`gh[opusr]_…`) tokens.
		// Fine-grained tokens are ~93 chars; classic are 40 chars after
		// the prefix. Run before generic-secret so `GH_TOKEN=ghp_…` is
		// captured by the github-token pattern rather than the
		// generic-secret fallback.
		{name: "github-token", expr: `gh[opusr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}`},
		// Slack bot / user / app / refresh / legacy-service tokens
		// (`xoxb-` / `xoxp-` / `xoxa-` / `xoxr-` / `xoxs-`). The `xoxs-`
		// prefix is broader than the four documented in Slack's current
		// API surface but covers legacy / third-party-issued tokens —
		// a redactor should err toward over-matching.
		{name: "slack-token", expr: `xox[baprs]-[A-Za-z0-9\-]{10,}`},
		{name: "bearer-token", expr: `(?i)bearer\s+[A-Za-z0-9\-_.~+/]+=*`},
		{name: "aws-access-key", expr: `AKIA[0-9A-Z]{16}`},
		// PR #233 review Nice-to-have #2: GCP service-account private
		// keys are emitted as PEM-style multi-line strings; the BEGIN
		// marker on its own is a leak signal that warrants alerting and
		// makes the entire payload unsafe to log verbatim. Match the
		// PEM header (with literal or `\n` body separator) so the
		// minimum forensic signature is scrubbed.
		{name: "gcp-private-key", expr: `-----BEGIN PRIVATE KEY-----[\s\S]*?(-----END PRIVATE KEY-----|$)`},
		// RFC 0053 — Google API keys (GEMINI_API_KEY / GOOGLE_API_KEY, and
		// GCP browser/server keys) carry the fixed `AIza` prefix followed by a
		// 35-char body over `[A-Za-z0-9_-]` (39 chars total). Run before
		// `generic-secret` so `GEMINI_API_KEY=AIza…` is captured by this
		// specific pattern rather than the generic fallback (the same ordering
		// the openai-api-key pattern relies on for `OPENAI_API_KEY=sk-…`).
		{name: "google-api-key", expr: `AIza[A-Za-z0-9_\-]{35}`},
		// PR #233 review MF-2: the previous `\S+` value class was greedy and
		// unbounded — on a JSON payload like
		// `{"password":"hunter2","next":"x"}` the match swallowed the closing
		// quote, comma, and the next field, both corrupting log parsers and
		// risking obscuring an adjacent secret-shaped value. Replace with a
		// bounded class that stops at JSON / shell delimiters and tolerate
		// the optional quotes around the separator that JSON uses
		// (`"key":"val"`). `[` is excluded from the value class so a second
		// pass cannot chew into a `[REDACTED:…]` marker emitted by an
		// earlier pattern. PR #233 deep-review L-1: also exclude `;` and
		// `&` so URL-encoded forms (`password=hunter2&next=foo`) and
		// shell-style key-value pairs (`password=hunter2; next=foo`) do not
		// over-redact the adjacent field.
		// PR #233 review Nice-to-have #7: trailing `["']?` consumes the
		// optional closing quote of a JSON / quoted-shell value so the
		// redacted output does not carry a stray `"` after the marker
		// (`{[REDACTED:generic-secret]","next":…}` → `{[REDACTED:generic-secret],"next":…}`).
		{name: "generic-secret", expr: `(?i)["']?(password|secret|token|api[_-]?key)["']?\s*[:=]\s*["']?[^\s,;&"'}\]\[]+["']?`},
	}
}

// AddPattern compiles expr and appends it to the redactor's pattern list.
// Returns an error if the regex fails to compile.
//
// Safe for concurrent use with [SecretRedactor.Redact] /
// [SecretRedactor.RedactStruct]: the pattern slice is guarded by an
// RWMutex (PR #233 review). In practice callers register all patterns at
// process startup; the lock is defensive against future hot-reload paths.
func (r *SecretRedactor) AddPattern(name, expr string) error {
	re, err := regexp.Compile(expr)
	if err != nil {
		return fmt.Errorf("security: compile redact pattern %q: %w", name, err)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
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
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.redactLocked(s)
}

// redactLocked is the unsynchronised redact body. The caller must hold
// r.mu (read or write). Used internally by Redact and by the reflective
// walk so a single RedactStruct call only takes the RLock once —
// re-entrant RLock acquisition can deadlock if a writer is queued between
// the outer and inner RLock (see sync.RWMutex docs).
func (r *SecretRedactor) redactLocked(s string) string {
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
// Opaque structs (returned as-is, no recursive walk) — RFC 0009 §I
// addendum, PR 1c: any struct type whose [isOpaqueStruct] check returns
// true. The rule covers `time.Time`, the `sync` primitives, and
// `atomic.Value` automatically; per-type registration is no longer
// required.
//
// Skipped: numeric types, unexported fields, channel/func/unsafe.
//
// Cycle and depth bound (PR #232 review SF-2 + PR #233 deep-review H-2):
// visited pointer addresses are tracked in a per-call set; recursion is
// capped at [reflectionDepthCap] levels. When the cap or a cycle fires:
//
//   - For a string field the value is replaced with
//     [redactDepthExceededMarker].
//   - For any other field type (struct, pointer, slice element, etc.) the
//     destination is left at its zero value. The previous behaviour
//     copied the original subtree across, which silently leaked any
//     secrets living past the cap (a struct nested 33 levels deep would
//     bypass redaction entirely).
//   - For map entries the marker is preserved if the element type can
//     hold it; otherwise the key is omitted.
//
// The input is not mutated. The returned value is a deep copy of every
// container the walk descends into; primitives are returned by value.
func (r *SecretRedactor) RedactStruct(v any) any {
	if v == nil {
		return nil
	}
	// Take the read lock once for the whole walk; the recursive helper
	// calls redactLocked (no lock) so we don't re-enter the RWMutex.
	r.mu.RLock()
	defer r.mu.RUnlock()
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
		return newDepthMarker()
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
			return newDepthMarker()
		}
		visited[addr] = struct{}{}
		defer delete(visited, addr)
		inner := r.walk(v.Elem(), depth+1, visited)
		if !inner.IsValid() {
			return v
		}
		// If the recursive call replaced the value with the typed sentinel
		// we cannot fit it back into the original pointer's type — return
		// the sentinel directly. The parent struct/slice/map switch then
		// matches via [isDepthMarker] and zeroes the destination instead
		// of re-copying the original (potentially secret-bearing) subtree
		// (PR #233 deep-review H-2).
		if isDepthMarker(inner) && v.Elem().Type().Kind() != reflect.String {
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
		// PR 1c: structural opaque-struct rule (RFC 0009 §I addendum).
		// Replaces the prior single-element type deny-list with a rule
		// that covers every standard-library hazard (`time.Time` /
		// `sync.*` / `atomic.Value` / chan-bearing types) without
		// per-type registration. See [isOpaqueStruct] for the rule.
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
			switch {
			case fieldOut.IsValid() && fieldOut.Type().AssignableTo(out.Field(i).Type()):
				out.Field(i).Set(fieldOut)
			case isDepthMarker(fieldOut):
				// PR #233 deep-review H-2: depth cap fired below this
				// non-string field. Leave the destination at its zero
				// value rather than copying the (potentially secret-
				// bearing) original subtree.
			default:
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
			switch {
			case elemOut.IsValid() && elemOut.Type().AssignableTo(out.Index(i).Type()):
				out.Index(i).Set(elemOut)
			case isDepthMarker(elemOut):
				// H-2: keep zero element rather than leaking original.
			default:
				out.Index(i).Set(v.Index(i))
			}
		}
		return out

	case reflect.Array:
		out := reflect.New(v.Type()).Elem()
		for i := 0; i < v.Len(); i++ {
			elemOut := r.walk(v.Index(i), depth+1, visited)
			switch {
			case elemOut.IsValid() && elemOut.Type().AssignableTo(out.Index(i).Type()):
				out.Index(i).Set(elemOut)
			case isDepthMarker(elemOut):
				// H-2: keep zero element rather than leaking original.
			default:
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
			switch {
			case valOut.IsValid() && valOut.Type().AssignableTo(out.Type().Elem()):
				out.SetMapIndex(iter.Key(), valOut)
			case isDepthMarker(valOut):
				// PR #233 deep-review H-2: drop the entry entirely
				// rather than re-inserting the unredacted original.
				// If the element type can hold a plain string
				// (any/string), preserve the user-visible marker
				// literal so operators can see the path was elided;
				// otherwise leave the key absent. The bare-string
				// emission here is intentional — internal sentinel
				// matching uses the typed [depthMarker] (see
				// [isDepthMarker]) but the audit-log boundary surfaces
				// the stable readable form for downstream consumers.
				userVisible := reflect.ValueOf(redactDepthExceededMarker)
				if userVisible.Type().AssignableTo(out.Type().Elem()) {
					out.SetMapIndex(iter.Key(), userVisible)
				}
			default:
				out.SetMapIndex(iter.Key(), iter.Value())
			}
		}
		return out

	case reflect.String:
		return reflect.ValueOf(r.redactLocked(v.String()))

	default:
		// Numbers, booleans, channels, funcs — return as-is.
		return v
	}
}

// isOpaqueStruct reports whether t must not be reflectively walked by
// [SecretRedactor.RedactStruct] (RFC 0009 §I addendum, PR 1c).
//
// A struct is opaque when either:
//
//  1. it has at least one unexported field whose type is not a primitive
//     (anything except [reflect.Bool], the integer kinds, the float kinds,
//     the complex kinds, [reflect.Uintptr], or [reflect.String]); or
//  2. it has no exported fields at all — the walk could not redact
//     anything but would still allocate a zeroed copy that loses the
//     original's unexported state.
//
// Rule (1) catches `time.Time` via its unexported `loc *Location`
// pointer field. PR #236 review L-2: `time.Time` actually carries
// three unexported fields (`wall uint64, ext int64, loc *Location`);
// `wall` and `ext` are primitives that on their own would not trip
// rule 1 — it is the pointer field that does. Rule (1) likewise
// catches `sync.Once` / `sync.WaitGroup` (unexported embedded structs)
// and `atomic.Value` (unexported `v any`). Rule (2) catches
// `sync.Mutex` (`state int32` / `sema uint32` are primitive but there
// is nothing exported to redact).
//
// Replacing the prior single-element deny-list (PR 1) means new caller
// surfaces — notably PR 3's tool-call argument structs — cannot
// accidentally route their unexported state through reflective copying.
// See RFC 0009 §I addendum for the alternatives that were considered
// (struct-tag opt-in / explicit allow-list) and rejected.
func isOpaqueStruct(t reflect.Type) bool {
	if t.Kind() != reflect.Struct {
		return false
	}
	hasExported := false
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if f.IsExported() {
			hasExported = true
			continue
		}
		if !isPrimitiveKind(f.Type.Kind()) {
			return true
		}
	}
	return !hasExported
}

// isPrimitiveKind reports whether k is a Go primitive for the purposes of
// the [isOpaqueStruct] rule. Pointers, interfaces, slices, maps, structs,
// arrays, channels, funcs, and unsafe pointers are non-primitive — each
// can carry references to state the reflective walk has no business
// mutating.
func isPrimitiveKind(k reflect.Kind) bool {
	switch k {
	case reflect.Bool,
		reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64,
		reflect.Uintptr,
		reflect.Float32, reflect.Float64,
		reflect.Complex64, reflect.Complex128,
		reflect.String:
		return true
	default:
		return false
	}
}
