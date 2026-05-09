package security

import "reflect"

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

// depthMarker is the typed-string sentinel returned by [walk] when the
// reflection depth or pointer-cycle cap fires. Distinct from a plain
// `string` so [isDepthMarker] can match on the reflect type rather than
// string content — caller data that happens to equal
// [redactDepthExceededMarker] byte-for-byte cannot false-positive
// (PR #233 review Nice-to-have #3 — versioned / non-printable depth-
// marker sentinel; the type discriminator is the version).
//
// JSON marshalling of `depthMarker` produces a plain string, so the
// audit-log byte stream is unchanged. The user-visible string emitted
// into [map[string]any] outputs is converted back to a bare string at
// the map-walk boundary so downstream consumers continue to observe a
// plain `string` value (audit log readers, JSON parsers, prometheus
// label sanitisers).
type depthMarker string

// depthMarkerType caches the reflect.Type used by [isDepthMarker] so
// hot-path matching does not re-resolve the type descriptor.
var depthMarkerType = reflect.TypeOf(depthMarker(""))

// isDepthMarker reports whether v carries the [depthMarker] sentinel.
// Used by the reflective walk to distinguish two cases where `walk`
// returns a value that is not assignable to the destination field type:
//
//  1. The walk hit the depth cap or a pointer cycle on a non-string field
//     and returned the sentinel. In that case the original subtree MUST
//     NOT be re-copied into the output — doing so would silently leak
//     any secrets that lived past the cap (PR #233 deep-review H-2).
//  2. The walk returned an unrelated invalid / non-assignable value (e.g.
//     a channel/func that walk returns as-is). In that case copying the
//     original is safe because the value cannot embed secrets reachable
//     by the regex pass.
//
// The type-keyed match (rather than string-content equality) makes
// case (1) detection robust against caller data that happens to equal
// the marker literal (PR #233 review Nice-to-have #3).
func isDepthMarker(v reflect.Value) bool {
	if !v.IsValid() {
		return false
	}
	return v.Type() == depthMarkerType
}

// newDepthMarker returns a [reflect.Value] wrapping the typed sentinel
// for emission from [walk] when the depth or cycle cap fires.
func newDepthMarker() reflect.Value {
	return reflect.ValueOf(depthMarker(redactDepthExceededMarker))
}
