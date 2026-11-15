// Package server — RFC 0018 PR 5: Server-Sent Events streaming for the
// `persatrix logs --follow` CLI flag.
//
// GET /api/v1/executions/{id}/logs/stream subscribes to the buffer's
// fan-out channel and emits one `data: <json>\n\n` SSE frame per new
// entry.  Heartbeats every 15s keep idle connections alive across
// proxies.  Subscriber cap exhaustion returns 429 Too Many Requests.
//
// The route reads the snapshot route's `since`, `workflow` and `level`
// (parseLogsFilter) and sends only entries that pass them.  With `since`
// it first sends the buffered entries the snapshot route would return
// for the same filters, then the live ones; without it the stream starts
// at the moment of connection.  `limit` is not read here.
package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// sseHeartbeatInterval is the cadence for SSE comment-frame heartbeats
// (`:\n\n`).  Set to 15s to stay below most reverse proxy idle timeouts
// (nginx default 60s, Cloudflare 100s) without flooding the stream.
const sseHeartbeatInterval = 15 * time.Second

// sseWriteTimeout caps how long a single SSE write may block on a
// stalled TCP peer.  Without it a dead client pins the subscriber slot
// indefinitely; with MaxSubscribersDefault=64 that lets ~64 dead
// clients soft-deny the SSE endpoint.  Issue #179 Should-Fix #3.
// Sized a few heartbeat intervals wide so a transient pause on a
// healthy client never trips the deadline.
const sseWriteTimeout = 30 * time.Second

// sseWrite applies the per-write deadline and writes a single chunk,
// returning any error.  ErrNotSupported from the ResponseController
// (test doubles, middleware without deadline support) is treated as
// best-effort: the write proceeds without a deadline rather than
// failing the whole stream.
//
// Deadline lifetime: SetWriteDeadline persists on the underlying
// net.Conn until reset or cleared.  We intentionally do not clear it
// when the caller's loop exits — the connection is closed by the
// http.Server on handler return, which releases any pending deadline
// along with the socket.  A subsequent flusher.Flush() issued by the
// caller runs under the most-recently-set deadline, which is the
// desired behavior (a slow peer on Flush should still trip the
// timeout rather than block indefinitely).  PR #182 review Low #2.
func sseWrite(rc *http.ResponseController, w http.ResponseWriter, p []byte) error {
	if err := rc.SetWriteDeadline(time.Now().Add(sseWriteTimeout)); err != nil &&
		!errors.Is(err, http.ErrNotSupported) {
		return err
	}
	_, err := w.Write(p)
	return err
}

// handleStreamLogs serves GET /api/v1/executions/{id}/logs/stream.
//
// TODO(RFC-0009): authenticate.  Same surface as the REST list
// endpoint; an unauthenticated caller can tail any execution's logs.
func (s *Server) handleStreamLogs(w http.ResponseWriter, r *http.Request) {
	if s.logBuffer == nil {
		writeError(w, "NOT_IMPLEMENTED", "log buffer not configured", http.StatusNotImplemented)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		writeError(w, "BAD_REQUEST", "execution_id is required", http.StatusBadRequest)
		return
	}
	filter, ok := parseLogsFilter(w, r.URL.Query())
	if !ok {
		return
	}
	// Translate the documented `_` wildcard into the buffer's
	// empty-string subscription convention.  Any other malformed ID is
	// rejected at Subscribe via ErrInvalidExecutionID below.
	subID := id
	if id == crossExecutionToken {
		subID = ""
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		// Should be impossible with net/http's default
		// ResponseWriter, but a future middleware that wraps the
		// writer without re-implementing Flusher would break SSE
		// silently.  Fail loudly rather than dribble buffered frames.
		writeError(w, "INTERNAL", "streaming unsupported by transport", http.StatusInternalServerError)
		return
	}

	// With `since` the buffered entries come with the subscription, taken
	// at the same moment, so none is missed or sent twice where the two
	// meet.  They are a copy: no buffer lock is held while they are
	// written to the client below.
	var (
		backlog []logbuffer.Entry
		ch      <-chan logbuffer.Entry
		cancel  func()
		err     error
	)
	if filter.since.IsZero() {
		ch, cancel, err = s.logBuffer.Subscribe(subID)
	} else {
		backlog, ch, cancel, err = s.logBuffer.SubscribeWithBacklog(subID)
	}
	if err != nil {
		switch {
		case errors.Is(err, logbuffer.ErrSubscriberCapExceeded):
			writeError(w, "TOO_MANY_REQUESTS", "subscriber cap exceeded", http.StatusTooManyRequests)
		case errors.Is(err, logbuffer.ErrInvalidExecutionID):
			writeError(w, "BAD_REQUEST", "invalid execution_id", http.StatusBadRequest)
		default:
			writeError(w, "INTERNAL", "failed to subscribe", http.StatusInternalServerError)
		}
		return
	}
	defer cancel()

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	// Disables proxy buffering on nginx without affecting other
	// proxies that ignore the header.
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	heartbeat := time.NewTicker(sseHeartbeatInterval)
	defer heartbeat.Stop()
	ctx := r.Context()
	rc := http.NewResponseController(w)

	// writeEntry sends one entry as an SSE frame under the write
	// deadline and reports whether the stream is still usable.
	writeEntry := func(entry logbuffer.Entry) bool {
		data, err := json.Marshal(entry)
		if err != nil {
			s.logger.Warn("sse: marshal entry failed", zap.Error(err))
			return true
		}
		for _, chunk := range [][]byte{[]byte("data: "), data, []byte("\n\n")} {
			if err := sseWrite(rc, w, chunk); err != nil {
				return false
			}
		}
		flusher.Flush()
		return true
	}

	// The same selection the snapshot route returns with no `limit`.
	for _, entry := range selectEntries(backlog, logsRequest{logsFilter: filter, limit: defaultLogsLimit}) {
		if ctx.Err() != nil || !writeEntry(entry) {
			return
		}
	}

	for {
		select {
		case <-ctx.Done():
			// Client disconnect — defer cancel() above removes the
			// subscription.
			return
		case <-heartbeat.C:
			// SSE comment frame; the colon prefix tells the EventSource
			// parser to ignore the line — pure keep-alive.
			if err := sseWrite(rc, w, []byte(": heartbeat\n\n")); err != nil {
				return
			}
			flusher.Flush()
		case entry, ok := <-ch:
			if !ok {
				// Subscription closed by buffer shutdown.
				return
			}
			if !filter.matches(entry) {
				continue
			}
			if !writeEntry(entry) {
				return
			}
		}
	}
}
