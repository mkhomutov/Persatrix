// Package server — RFC 0018 PR 5: REST endpoint for log retrieval.
//
// GET /api/v1/executions/{id}/logs returns the orchestrator's snapshot
// of the named execution's log entries (oldest first), filtered by the
// optional `since` / `workflow` / `level` / `limit` query params.
//
// The special path id=_ performs a chronological merge across every
// known execution; `limit` (capped at maxLogsLimit) is enforced after
// the merge.
//
// Authentication is deferred — see the TODO marker below.
package server

import (
	"errors"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// crossExecutionToken is the path-segment value used to request the
// merged cross-execution view.  Documented as `id=_` in the RFC and
// CLI; chosen because it is reserved by the buffer's validator
// (reservedExecutionIDs) so a producer cannot Append with
// ExecutionID="_" and shadow the merged view.  Issue #179 Should-Fix #1.
const crossExecutionToken = "_"

// maxLogsLimit caps `limit` to keep a single response from holding the
// merged contents of the entire ring fleet in memory.  RFC 0018 § E
// pins the per-execution ring at 1000 entries; 5000 covers a generous
// `id=_ --limit=5000` cross-merge without an unbounded fan-in.
const maxLogsLimit = 5000

// defaultLogsLimit applies when the caller omits `limit`.  Matches
// the per-execution ring cap so a default request returns the full
// in-memory snapshot for a single execution.
const defaultLogsLimit = 1000

// logsRequest holds the parsed + validated query params.
type logsRequest struct {
	since    time.Time // zero ⇒ no lower bound
	workflow string    // "" ⇒ no filter (matched against attributes["workflow"])
	level    string    // "" ⇒ no filter; uppercase
	limit    int
}

// handleListLogs serves GET /api/v1/executions/{id}/logs.
//
// TODO(RFC-0009): authenticate.  This endpoint exposes execution log
// payloads which may include LLM prompts and tool inputs that future
// redactor implementations will scrub but today emit verbatim.
func (s *Server) handleListLogs(w http.ResponseWriter, r *http.Request) {
	if s.logBuffer == nil {
		writeError(w, "NOT_IMPLEMENTED", "log buffer not configured", http.StatusNotImplemented)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		writeError(w, "BAD_REQUEST", "execution_id is required", http.StatusBadRequest)
		return
	}
	req, ok := parseLogsRequest(w, r)
	if !ok {
		return
	}

	var entries []logbuffer.Entry
	if id == crossExecutionToken {
		entries = s.collectAllEntries()
	} else {
		entries = s.logBuffer.Snapshot(id)
		if entries == nil {
			// Snapshot returns nil for invalid + unknown IDs.  We
			// distinguish here so a malformed ID gets a 400 (matches
			// the agent + workflow handlers) while an unknown but
			// well-formed ID returns an empty 200 (clients commonly
			// poll before the first entry lands).
			if !logbuffer.ValidExecutionID(id) {
				writeError(w, "BAD_REQUEST", "invalid execution_id", http.StatusBadRequest)
				return
			}
			entries = []logbuffer.Entry{}
		}
	}

	entries = filterEntries(entries, req)
	sort.SliceStable(entries, func(i, j int) bool {
		return entries[i].Timestamp.Before(entries[j].Timestamp)
	})
	if len(entries) > req.limit {
		entries = entries[len(entries)-req.limit:]
	}
	writeJSON(w, entries, http.StatusOK)
}

// collectAllEntries gathers entries from every known execution for the
// id=_ merged view.  Entries are sorted post-merge by handleListLogs.
func (s *Server) collectAllEntries() []logbuffer.Entry {
	ids := s.logBuffer.ListExecutions()
	var out []logbuffer.Entry
	for _, id := range ids {
		out = append(out, s.logBuffer.Snapshot(id)...)
	}
	return out
}

// parseLogsRequest validates query params and writes a 400 on any
// malformed value.
func parseLogsRequest(w http.ResponseWriter, r *http.Request) (logsRequest, bool) {
	q := r.URL.Query()
	req := logsRequest{limit: defaultLogsLimit}

	if raw := q.Get("since"); raw != "" {
		t, err := parseSince(raw, time.Now())
		if err != nil {
			writeError(w, "BAD_REQUEST", "invalid since: "+err.Error(), http.StatusBadRequest)
			return req, false
		}
		req.since = t
	}
	req.workflow = q.Get("workflow")
	if raw := q.Get("level"); raw != "" {
		up := strings.ToUpper(raw)
		switch up {
		case "DEBUG", "INFO", "WARN", "ERROR":
			req.level = up
		default:
			writeError(w, "BAD_REQUEST", "invalid level (DEBUG|INFO|WARN|ERROR)", http.StatusBadRequest)
			return req, false
		}
	}
	if raw := q.Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n <= 0 {
			writeError(w, "BAD_REQUEST", "invalid limit", http.StatusBadRequest)
			return req, false
		}
		if n > maxLogsLimit {
			n = maxLogsLimit
		}
		req.limit = n
	}
	return req, true
}

// parseSince accepts either a Go duration ("5m", "1h30m") interpreted
// as "now - duration", or an RFC 3339 timestamp.  The caller passes
// `now` so tests can pin a deterministic clock.
//
// Negative durations (e.g. "-5m") are rejected: time.ParseDuration
// accepts them, but they would silently translate to a future `since`
// timestamp and the filter would always evict everything — surfacing
// the typo as an empty 200 instead of a 400 (PR #173 review Should-Fix #1).
//
// Future-dated RFC 3339 timestamps are rejected for the same reason
// (PR #173 review Should-Fix #3): a typo like "2099-01-01T..." would
// otherwise return an always-empty 200.
func parseSince(raw string, now time.Time) (time.Time, error) {
	if d, err := time.ParseDuration(raw); err == nil {
		if d < 0 {
			return time.Time{}, errors.New("duration must be non-negative")
		}
		return now.Add(-d), nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, err
	}
	if t.After(now) {
		return time.Time{}, errors.New("timestamp must not be in the future")
	}
	return t, nil
}

// filterEntries applies the parsed request filters in-place semantics.
// Returns a fresh slice so the caller can mutate (sort / truncate)
// without aliasing the buffer's snapshot.
func filterEntries(in []logbuffer.Entry, req logsRequest) []logbuffer.Entry {
	out := make([]logbuffer.Entry, 0, len(in))
	for _, e := range in {
		if !req.since.IsZero() && e.Timestamp.Before(req.since) {
			continue
		}
		if req.level != "" && !levelMatch(e.Level, req.level) {
			continue
		}
		if req.workflow != "" {
			if v, ok := e.Attributes["workflow"]; !ok || asString(v) != req.workflow {
				continue
			}
		}
		out = append(out, e)
	}
	return out
}

// levelMatch is a simple equality check — the API documents the filter
// as "exact match", not a minimum severity, because operators
// typically want to grep for one severity at a time and a `>=`
// semantics would surprise anyone running `level=ERROR`.
func levelMatch(have, want string) bool {
	return strings.EqualFold(have, want)
}

func asString(v any) string {
	s, _ := v.(string)
	return s
}
