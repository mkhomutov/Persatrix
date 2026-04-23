// Package server — RFC 0018 PR 5: Server-Sent Events streaming for the
// `persatrix logs --follow` CLI flag.
//
// GET /api/v1/executions/{id}/logs/stream subscribes to the buffer's
// fan-out channel and emits one `data: <json>\n\n` SSE frame per new
// entry.  Heartbeats every 15s keep idle connections alive across
// proxies.  Subscriber cap exhaustion returns 429 Too Many Requests.
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

	ch, cancel, err := s.logBuffer.Subscribe(subID)
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

	for {
		select {
		case <-ctx.Done():
			// Client disconnect — defer cancel() above removes the
			// subscription.
			return
		case <-heartbeat.C:
			// SSE comment frame; the colon prefix tells the EventSource
			// parser to ignore the line — pure keep-alive.
			if _, err := w.Write([]byte(": heartbeat\n\n")); err != nil {
				return
			}
			flusher.Flush()
		case entry, ok := <-ch:
			if !ok {
				// Subscription closed by buffer shutdown.
				return
			}
			data, err := json.Marshal(entry)
			if err != nil {
				s.logger.Warn("sse: marshal entry failed", zap.Error(err))
				continue
			}
			if _, err := w.Write([]byte("data: ")); err != nil {
				return
			}
			if _, err := w.Write(data); err != nil {
				return
			}
			if _, err := w.Write([]byte("\n\n")); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}
