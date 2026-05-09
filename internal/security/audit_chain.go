package security

import (
	"bytes"
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
// and reports (priorTailRaw, priorChecksum, recoveryKind, recoveryReason).
//
// On any error short of "file does not exist" the function returns
// recoveryRecovered so the caller emits chain.recovered — the design
// intentionally fails loudly rather than silently rolling forward.
//
// PR #233 deep-review L-4: recoveryReason carries the underlying I/O or
// parse-failure message so the synthetic chain.recovered event records
// *why* recovery fired (permission denied vs malformed JSON vs truncated
// tail). Empty for the bootstrap and restart paths.
func inspectTail(path string) (priorTail string, prevSum string, kind recoveryKind, reason string) {
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", emptyChecksum(), recoveryBootstrap, ""
		}
		return "", emptyChecksum(), recoveryRecovered, fmt.Sprintf("stat: %v", err)
	}
	if info.Size() == 0 {
		return "", emptyChecksum(), recoveryBootstrap, ""
	}

	f, err := os.Open(path)
	if err != nil {
		return "", emptyChecksum(), recoveryRecovered, fmt.Sprintf("open: %v", err)
	}
	defer f.Close()

	last, ok := readLastLine(f)
	if !ok {
		// PR #233 review SF-1: forward the partial bytes so
		// emitRecoveryEvent can persist them as
		// `prior_tail_raw_truncated`. Previously we returned "" here, which
		// meant the forensic field was always empty for the truncated-tail
		// case it was designed for.
		return last, emptyChecksum(), recoveryRecovered, "tail-read: incomplete final line (truncated or oversized record)"
	}

	var ev AuditEvent
	if err := json.Unmarshal([]byte(last), &ev); err != nil {
		return last, emptyChecksum(), recoveryRecovered, fmt.Sprintf("tail-parse: %v", err)
	}
	if !looksLikeSHA256(ev.Checksum) {
		return last, emptyChecksum(), recoveryRecovered, "tail-parse: missing or malformed checksum field"
	}
	return last, ev.Checksum, recoveryRestart, ""
}

// readLastLine returns the last newline-terminated line in r (without the
// trailing newline). Returns ok=false if r contains no complete line.
//
// The tail window is sized at 1 MiB so any realistic AuditEvent.Detail
// payload fits inside it. PR #233 review flagged the prior 64 KiB cap as
// asymmetric with the unbounded `Detail` map: any single serialised event
// exceeding the window would misclassify a clean tail as truncated and
// emit a spurious `chain.recovered` on every restart. 1 MiB keeps the
// per-startup read cheap (one syscall, well under any practical event
// size) while leaving headroom for adversarial / very-detailed events.
//
// PR #233 deep-review M-2: when the read is window-bounded (start > 0)
// and the window contains no internal newline, the last record is itself
// larger than tailWindow — the bytes we hold are only its suffix. We
// return ok=false in that case so the caller emits chain.recovered rather
// than treating a record fragment as a complete line (which would chain
// the next event onto a truncated checksum and silently corrupt the log).
func readLastLine(r io.ReadSeeker) (string, bool) {
	const tailWindow = 1 << 20 // 1 MiB; see PR #233 review (was 64 KiB).
	size, err := r.Seek(0, io.SeekEnd)
	if err != nil {
		return "", false
	}
	start := size - tailWindow
	if start < 0 {
		start = 0
	}
	windowed := start > 0
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
		// Last line is unterminated → treat as truncated tail. Return only
		// the bytes after the last complete newline so the forensic record
		// (`prior_tail_raw_truncated`, PR #233 review SF-1) carries just the
		// partial last line rather than the whole tail window.
		if i := bytes.LastIndexByte(buf, '\n'); i >= 0 {
			return string(buf[i+1:]), false
		}
		return string(buf), false
	}
	if i := bytes.LastIndexByte(buf, '\n'); i >= 0 {
		return string(buf[i+1:]), true
	}
	// PR #233 deep-review M-2: no internal newline. If the read was
	// windowed (start > 0), the bytes we have are a suffix of a record
	// larger than tailWindow — fail loudly so the caller emits
	// chain.recovered rather than chaining onto a record fragment.
	if windowed {
		return string(buf), false
	}
	return string(buf), len(buf) > 0
}

// looksLikeSHA256 reports whether s parses as a 32-byte (64-hex-char)
// SHA-256 digest. PR #233 review Should-Fix #7: the prior hand-rolled
// loop was redundant — [hex.DecodeString] already validates the
// alphabet and surfaces a typed error. The standard library accepts
// both upper- and lowercase hex; the audit chain only emits lowercase
// via [hex.EncodeToString], so the broader acceptance only matters
// when an operator hand-edits the file (already off-spec) and we
// prefer to accept their checksum over emitting a spurious
// chain.recovered.
func looksLikeSHA256(s string) bool {
	if len(s) != 64 {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

func emptyChecksum() string {
	sum := sha256.Sum256(nil)
	return hex.EncodeToString(sum[:])
}

// fileEndsWithoutNewline reports whether path's last byte is not '\n'.
// Returns false on any error (including non-existent path) — callers only
// invoke this after recovery detected a truncated tail, so the file exists.
//
// PR #233 deep-review L-3: this opens the file a second time while
// NewFileAuditLogger is already holding an O_APPEND handle. The orchestrator
// is the sole writer to the audit file, so concurrent mutation between the
// two opens is not possible in practice. If a future change adds a second
// writer (e.g. external log rotator) this assumption breaks; consider
// refactoring to share a single handle via Stat()+Pread on the existing
// file descriptor at that point.
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

func (l *fileAuditLogger) emitRecoveryEvent(kind recoveryKind, tail string, reason string) error {
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
		// PR #233 deep-review L-4: surface the underlying recovery reason
		// (permission failure, parse failure, oversized tail, etc.) so an
		// operator inspecting chain.recovered events can distinguish a
		// benign restart-after-crash from a real integrity incident.
		detail := map[string]any{"prior_tail": "unknown", "prior_tail_raw_truncated": truncate(tail, 256)}
		if reason != "" {
			detail["recovery_reason"] = reason
		}
		ev = AuditEvent{
			EventType: AuditChainRecovered,
			Action:    "recovered",
			Outcome:   "warn",
			Detail:    detail,
		}
		// PR 1c — dedicated chain-recovery counter so operators can
		// alert on integrity events without needing to slice
		// audit_events_total{event_type=...}. Bumped *before* Emit so
		// the count survives even if Emit's downstream write fails;
		// the counter is monotonic and a duplicate-on-retry would
		// exaggerate by one which is preferable to under-counting an
		// integrity incident.
		l.metrics.RecordChainRecovered()
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
//
// SECURITY NOTE (PR #233 deep-review L-2): the chain is unauthenticated
// SHA-256 — it provides tamper *evidence* (accidental corruption,
// truncation, partial writes will mismatch on re-validation) but NOT
// tamper *resistance*. Anyone with write access to the audit file can
// recompute the entire chain from scratch and produce a self-consistent
// forgery. RFC 0009 §G accepts this trade-off because non-repudiation
// belongs to the off-host SIEM forwarding path (v0.4.0). Future readers
// should not mistake the chain for an HMAC — adding a key here without
// also solving secure key storage would be cargo-cult security.
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

// VerifyChain re-reads path and recomputes the per-event sha256
// checksum chain, returning the first error that breaks the chain. A
// nil return means every event's recorded Checksum matches the value
// computed from canonicalEventJSON(prev, ev). Used by external
// auditors and the future `persatrix audit verify` CLI subcommand
// (PR #233 review Nice-to-have #1) so callers do not re-implement
// [canonicalEventJSON].
//
// Errors surface the line number (1-indexed) and the underlying
// reason: malformed JSON, missing/short Checksum, or a recomputed
// hash that disagrees with the recorded one. Empty / missing files
// are reported via a typed [os.PathError]-class error from
// [os.Open] — callers running the verifier against a wrong path see
// the failure rather than a false-positive "clean" verdict.
//
// Synthetic chain.bootstrap / chain.restart / chain.recovered events
// participate in the chain like any other event; truncated files
// produce a chain.recovered written by the next [NewFileAuditLogger]
// open, so a verifier run shortly after restart should validate clean
// against the post-restart prefix even when the pre-restart suffix
// was corrupt.
func VerifyChain(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("security: open audit log %q: %w", path, err)
	}
	defer f.Close()

	dec := json.NewDecoder(f)
	prevSum := emptyChecksum()
	lineNo := 0
	for dec.More() {
		lineNo++
		var ev AuditEvent
		if err := dec.Decode(&ev); err != nil {
			return fmt.Errorf("security: verify audit log line %d: decode: %w", lineNo, err)
		}
		if !looksLikeSHA256(ev.Checksum) {
			return fmt.Errorf("security: verify audit log line %d: missing or malformed checksum %q", lineNo, ev.Checksum)
		}
		recorded := ev.Checksum
		canonical, err := canonicalEventJSON(ev, prevSum)
		if err != nil {
			return fmt.Errorf("security: verify audit log line %d: canonicalise: %w", lineNo, err)
		}
		sum := sha256.Sum256(canonical)
		got := hex.EncodeToString(sum[:])
		if got != recorded {
			return fmt.Errorf("security: verify audit log line %d: checksum mismatch (recorded=%s computed=%s)", lineNo, recorded, got)
		}
		prevSum = recorded
	}
	return nil
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
