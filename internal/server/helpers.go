package server

import (
	"encoding/json"
	"errors"
	"net/http"
)

// writeJSON marshals v to JSON and writes it with the given status code.
func writeJSON(w http.ResponseWriter, v any, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// writeError writes a JSON error envelope with the given code, message, and HTTP status.
func writeError(w http.ResponseWriter, code string, msg string, status int) {
	writeJSON(w, errorResponse{Error: msg, Code: code}, status)
}

// requireJSON checks that the request Content-Type is application/json.
// Returns true if valid; writes a 400 error and returns false otherwise.
func requireJSON(w http.ResponseWriter, r *http.Request) bool {
	ct := r.Header.Get("Content-Type")
	if ct != "application/json" {
		writeError(w, "BAD_REQUEST", "Content-Type must be application/json", http.StatusBadRequest)
		return false
	}
	return true
}

// decodeJSON reads the request body (with a 1 MiB limit), decodes strict JSON
// into dst, and returns true on success. On failure it writes the appropriate
// error response and returns false.
func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20) // 1 MiB
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		var maxBytesErr *http.MaxBytesError
		if errors.As(err, &maxBytesErr) {
			writeError(w, "BAD_REQUEST", "request body too large", http.StatusBadRequest)
			return false
		}
		writeError(w, "BAD_REQUEST", "invalid JSON: "+err.Error(), http.StatusBadRequest)
		return false
	}
	return true
}
