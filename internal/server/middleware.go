package server

import (
	"context"
	"net/http"
	"runtime/debug"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"
)

// contextKey is an unexported type for context value keys to avoid collisions (SA1029).
type contextKey string

const requestIDKey contextKey = "request_id"

// recoveryMiddleware catches handler panics, logs the stack trace, and returns
// a 500 JSON error instead of crashing the connection.
func recoveryMiddleware(logger *zap.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				logger.Error("handler panic",
					zap.Any("panic", rec),
					zap.String("stack", string(debug.Stack())),
				)
				writeError(w, "INTERNAL", "internal server error", http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// requestIDMiddleware generates a server-side UUID for every request and sets
// it as X-Request-ID response header and in the request context. Client-provided
// X-Request-ID is intentionally ignored to prevent log injection attacks.
func requestIDMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := uuid.NewString()
		w.Header().Set("X-Request-ID", id)
		ctx := context.WithValue(r.Context(), requestIDKey, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// statusCapture wraps http.ResponseWriter to record the written status code.
// NOTE (Review finding F-05): implements http.Flusher conditionally to forward
// Flush() calls to the underlying writer, preventing breakage when v0.2 SSE
// streaming handlers type-assert to http.Flusher.
type statusCapture struct {
	http.ResponseWriter
	status int
}

func (sc *statusCapture) WriteHeader(code int) {
	sc.status = code
	sc.ResponseWriter.WriteHeader(code)
}

// Flush implements http.Flusher by delegating to the underlying ResponseWriter
// if it supports flushing. This prevents silent failures or panics when
// downstream handlers (e.g., SSE in v0.2) type-assert to http.Flusher.
func (sc *statusCapture) Flush() {
	if f, ok := sc.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// loggingMiddleware logs method, path, status code, latency, and request ID
// for every completed request.
func loggingMiddleware(logger *zap.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sc := &statusCapture{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sc, r)
		reqID, _ := r.Context().Value(requestIDKey).(string)
		// (Review finding F-06): Log 5xx responses at Warn level for operator alerting;
		// all other responses at Info level.
		if sc.status >= 500 {
			logger.Warn("http request",
				zap.String("method", r.Method),
				zap.String("path", r.URL.Path),
				zap.Int("status", sc.status),
				zap.Duration("latency", time.Since(start)),
				zap.String("request_id", reqID),
			)
		} else {
			logger.Info("http request",
				zap.String("method", r.Method),
				zap.String("path", r.URL.Path),
				zap.Int("status", sc.status),
				zap.Duration("latency", time.Since(start)),
				zap.String("request_id", reqID),
			)
		}
	})
}
