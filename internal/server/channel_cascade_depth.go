package server

import (
	"fmt"
	"math"
)

// validateRequestCascadeDepth enforces the publisher-side invariants on
// `metadata.cascade_depth` that the router-side clamp cannot recover:
// the value (when present) MUST be a whole, non-negative number. JSON
// unmarshal into `map[string]any` yields `float64` for every numeric,
// so the handler MUST verify "whole" downstream of decode rather than
// at the codec layer. Over-cap values are NOT rejected here — the
// publisher does not know the deployment's cap, so over-cap values
// are silently clamped server-side at the router boundary.
//
// Returns (errMessage, false) when the request must be rejected;
// (_, true) when the metadata bag is acceptable (absent or valid).
//
// RFC 0011 amendment 'Cascade-depth wire propagation':
// docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
func validateRequestCascadeDepth(metadata map[string]any) (string, bool) {
	if metadata == nil {
		return "", true
	}
	raw, ok := metadata["cascade_depth"]
	if !ok {
		return "", true
	}
	var asFloat float64
	switch v := raw.(type) {
	case float64:
		asFloat = v
	case float32:
		asFloat = float64(v)
	case int:
		asFloat = float64(v)
	case int32:
		asFloat = float64(v)
	case int64:
		asFloat = float64(v)
	default:
		return fmt.Sprintf("metadata.cascade_depth: must be a non-negative integer (got %T)", raw), false
	}
	if math.IsNaN(asFloat) || math.IsInf(asFloat, 0) {
		return "metadata.cascade_depth: must be a finite non-negative integer", false
	}
	if asFloat < 0 {
		return fmt.Sprintf("metadata.cascade_depth: must be non-negative (got %v)", asFloat), false
	}
	if math.Trunc(asFloat) != asFloat {
		return fmt.Sprintf("metadata.cascade_depth: must be a whole number (got %v)", asFloat), false
	}
	return "", true
}
