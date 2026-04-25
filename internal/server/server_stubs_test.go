package server

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
)

// =============================================================================
// Stub Endpoint Tests (Phase 3)
// =============================================================================

func TestGetLogsStub(t *testing.T) {
	srv, _ := testServer(t)
	// Without a logbuffer wired in, the endpoint reports
	// NOT_IMPLEMENTED (RFC 0018 PR 5: the buffer is now optional
	// via WithLogBuffer; without it the endpoint surfaces the
	// configuration gap rather than silently returning [] ).
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/any-id/logs", nil)
	assert.Equal(t, http.StatusNotImplemented, rec.Code)
	assert.Contains(t, rec.Body.String(), "NOT_IMPLEMENTED")
	assert.Contains(t, rec.Body.String(), "log buffer not configured")
}

func TestGetCostSummary_NoCostReporter(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)
	assert.Contains(t, rec.Body.String(), "SERVICE_UNAVAILABLE")
	assert.Contains(t, rec.Body.String(), "cost tracking is not configured")
}

// NOTE(review-F09): Wrong-method tests document the HTTP method contract for
// stub endpoints. Go 1.22+ ServeMux pattern routing handles 405 automatically.

func TestLogsStubWrongMethod(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/executions/any-id/logs", nil)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestCostSummaryStubWrongMethod(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}
