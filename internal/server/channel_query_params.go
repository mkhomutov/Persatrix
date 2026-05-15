package server

// Query-parameter parsers for the channel REST endpoints. Split from
// channel_handlers.go to keep that file under the 500-line review cap.

import (
	"fmt"
	"net/http"
	"strconv"
	"time"
)

// parseLimit parses the optional `?limit=` query parameter, returning
// `fallback` when absent. PR #245 review (Low): a non-empty malformed
// value (`abc`, `-5`, `0`) used to be silently coerced to the fallback.
// That hides client bugs and conflicts with the parseBefore convention
// just below (which errors loudly on a malformed `?before=`). We now
// return an error so the caller can surface 400 BAD_REQUEST. Values
// above [channelMaxLimit] are still capped silently — that is the
// documented contract (the cap exists to bound allocation, not to
// signal a client bug).
func parseLimit(r *http.Request, fallback int) (int, error) {
	raw := r.URL.Query().Get("limit")
	if raw == "" {
		return fallback, nil
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("limit must be a positive integer: %s", raw)
	}
	if v <= 0 {
		return 0, fmt.Errorf("limit must be a positive integer: %d", v)
	}
	if v > channelMaxLimit {
		return channelMaxLimit, nil
	}
	return v, nil
}

// parseBefore parses the optional `before` cursor as RFC 3339. Returns
// the zero value (sentinel for "now") when the parameter is absent.
// Errors out on a malformed value rather than silently treating it as
// "now" — drift between the cursor format and the response timestamp
// format would be hard to debug.
func parseBefore(r *http.Request) (time.Time, error) {
	raw := r.URL.Query().Get("before")
	if raw == "" {
		return time.Time{}, nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, fmt.Errorf("before must be RFC 3339: %w", err)
	}
	return t.UTC(), nil
}
