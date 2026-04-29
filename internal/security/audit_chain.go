package security

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"time"
)

// recoveryKind enumerates the three startup outcomes for the audit log
// chain (PR #232 review SF-3).
type recoveryKind int

const (
	recoveryBootstrap recoveryKind = iota
	recoveryRestart
	recoveryRecovered
)

// inspectTail reads the last newline-terminated record from path (if any)
// and reports (priorTailRaw, priorChecksum, recoveryKind).
//
// On any error short of "file does not exist" the function returns
// recoveryRecovered so the caller emits chain.recovered — the design
// intentionally fails loudly rather than silently rolling forward.
func inspectTail(path string) (priorTail string, prevSum string, kind recoveryKind) {
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", emptyChecksum(), recoveryBootstrap
		}
		return "", emptyChecksum(), recoveryRecovered
	}
	if info.Size() == 0 {
		return "", emptyChecksum(), recoveryBootstrap
	}

	f, err := os.Open(path)
	if err != nil {
		return "", emptyChecksum(), recoveryRecovered
	}
	defer f.Close()

	last, ok := readLastLine(f)
	if !ok {
		return "", emptyChecksum(), recoveryRecovered
	}

	var ev AuditEvent
	if err := json.Unmarshal([]byte(last), &ev); err != nil {
		return last, emptyChecksum(), recoveryRecovered
	}
	if !looksLikeSHA256(ev.Checksum) {
		return last, emptyChecksum(), recoveryRecovered
	}
	return last, ev.Checksum, recoveryRestart
}

// readLastLine returns the last newline-terminated line in r (without the
// trailing newline). Returns ok=false if r contains no complete line.
func readLastLine(r io.ReadSeeker) (string, bool) {
	const tailWindow = 64 * 1024
	size, err := r.Seek(0, io.SeekEnd)
	if err != nil {
		return "", false
	}
	start := size - tailWindow
	if start < 0 {
		start = 0
	}
	if _, err := r.Seek(start, io.SeekStart); err != nil {
		return "", false
	}
	buf, err := io.ReadAll(r)
	if err != nil {
		return "", false
	}
	if n := len(buf); n > 0 && buf[n-1] == '\n' {
		buf = buf[:n-1]
	} else if len(buf) > 0 {
		// Last line is unterminated → treat as truncated tail.
		return string(buf), false
	}
	if i := lastIndexByte(buf, '\n'); i >= 0 {
		return string(buf[i+1:]), true
	}
	return string(buf), len(buf) > 0
}

func lastIndexByte(b []byte, c byte) int {
	for i := len(b) - 1; i >= 0; i-- {
		if b[i] == c {
			return i
		}
	}
	return -1
}

func looksLikeSHA256(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= '0' && c <= '9':
		case c >= 'a' && c <= 'f':
		default:
			return false
		}
	}
	return true
}

func emptyChecksum() string {
	sum := sha256.Sum256(nil)
	return hex.EncodeToString(sum[:])
}

// fileEndsWithoutNewline reports whether path's last byte is not '\n'.
// Returns false on any error (including non-existent path) — callers only
// invoke this after recovery detected a truncated tail, so the file exists.
func fileEndsWithoutNewline(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	if _, err := f.Seek(-1, io.SeekEnd); err != nil {
		return false
	}
	var b [1]byte
	if _, err := f.Read(b[:]); err != nil {
		return false
	}
	return b[0] != '\n'
}

func (l *fileAuditLogger) emitRecoveryEvent(kind recoveryKind, tail string) error {
	var ev AuditEvent
	switch kind {
	case recoveryBootstrap:
		ev = AuditEvent{EventType: AuditChainBootstrap, Action: "bootstrap", Outcome: "ok"}
	case recoveryRestart:
		ev = AuditEvent{
			EventType: AuditChainRestart,
			Action:    "restart",
			Outcome:   "ok",
			Detail:    map[string]any{"prior_tail_checksum": l.prevChecksum},
		}
	case recoveryRecovered:
		ev = AuditEvent{
			EventType: AuditChainRecovered,
			Action:    "recovered",
			Outcome:   "warn",
			Detail:    map[string]any{"prior_tail": "unknown", "prior_tail_raw_truncated": truncate(tail, 256)},
		}
	}
	return l.Emit(context.Background(), ev)
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "…"
}

// canonicalEventJSON returns the deterministic byte representation of ev that
// feeds the checksum chain. The Checksum field is omitted; all other fields
// are emitted in alphabetical key order with the prevChecksum prefixed as a
// length-tagged segment so an attacker cannot construct a colliding event by
// shifting bytes between fields.
func canonicalEventJSON(ev AuditEvent, prevChecksum string) ([]byte, error) {
	m := map[string]any{
		"timestamp":      ev.Timestamp.UTC().Format(time.RFC3339Nano),
		"correlation_id": ev.CorrelationID,
		"event_type":     string(ev.EventType),
		"agent_id":       ev.AgentID,
		"action":         ev.Action,
		"resource":       ev.Resource,
		"outcome":        ev.Outcome,
	}
	if ev.Detail != nil {
		m["detail"] = sortMap(ev.Detail)
	}

	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	buf := make([]byte, 0, 256)
	buf = append(buf, fmt.Sprintf("prev=%d:%s|", len(prevChecksum), prevChecksum)...)
	for _, k := range keys {
		v, err := json.Marshal(m[k])
		if err != nil {
			return nil, fmt.Errorf("security: canonicalise field %q: %w", k, err)
		}
		buf = append(buf, k...)
		buf = append(buf, '=')
		buf = append(buf, v...)
		buf = append(buf, '|')
	}
	return buf, nil
}

func sortMap(m map[string]any) any {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([][2]any, 0, len(keys))
	for _, k := range keys {
		out = append(out, [2]any{k, m[k]})
	}
	return out
}
