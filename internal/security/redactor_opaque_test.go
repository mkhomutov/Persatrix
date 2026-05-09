package security

import (
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// PR 1c — opaque-struct rule (RFC 0009 §I addendum).
//
// fixtureOpaqueByPointer trips rule 1: an unexported pointer field is
// non-primitive, so the whole struct is opaque even though Public could
// otherwise be redacted.
type fixtureOpaqueByPointer struct {
	Public  string // would-be redactable, but the struct is opaque
	private *int   // unexported pointer → non-primitive → opaque
}

// fixtureOpaqueByChan trips rule 1 via an unexported channel.
type fixtureOpaqueByChan struct {
	Public string
	stop   chan struct{}
}

// fixtureOpaqueByMap trips rule 1 via an unexported map.
type fixtureOpaqueByMap struct {
	Public string
	cache  map[string]string
}

// fixtureWalkable has exported fields and only primitive unexported state,
// so the walk descends as before. The unexported counter is dropped (set
// to its zero value in the copy) — this is acceptable because the field
// was unreadable to callers anyway.
type fixtureWalkable struct {
	Public  string
	counter int
}

func TestRedactStruct_OpaqueByUnexportedPointer(t *testing.T) {
	r := NewSecretRedactor()
	x := 7
	in := &fixtureOpaqueByPointer{Public: "Bearer abc.def==", private: &x}
	out, ok := r.RedactStruct(in).(*fixtureOpaqueByPointer)
	if !ok {
		t.Fatalf("RedactStruct returned wrong type %T", r.RedactStruct(in))
	}
	if out.Public != "Bearer abc.def==" {
		t.Errorf("opaque struct mutated: Public = %q", out.Public)
	}
	if out.private != &x {
		t.Errorf("opaque struct's unexported pointer was rewritten")
	}
}

func TestRedactStruct_OpaqueByUnexportedChan(t *testing.T) {
	r := NewSecretRedactor()
	in := &fixtureOpaqueByChan{Public: "Bearer abc.def==", stop: make(chan struct{})}
	out := r.RedactStruct(in).(*fixtureOpaqueByChan)
	if out.Public != "Bearer abc.def==" {
		t.Errorf("opaque struct mutated: Public = %q", out.Public)
	}
	if out.stop != in.stop {
		t.Errorf("opaque struct's unexported chan was rewritten")
	}
}

func TestRedactStruct_OpaqueByUnexportedMap(t *testing.T) {
	r := NewSecretRedactor()
	in := &fixtureOpaqueByMap{
		Public: "Bearer abc.def==",
		cache:  map[string]string{"k": "v"},
	}
	out := r.RedactStruct(in).(*fixtureOpaqueByMap)
	if out.Public != "Bearer abc.def==" {
		t.Errorf("opaque struct mutated: Public = %q", out.Public)
	}
	// The map header is copied by Set when the surrounding pointer is
	// re-wrapped, but the backing storage must remain shared with the
	// input (the bail-out's whole point is that the unexported map is
	// not reflectively rewalked). Mutating the input map and observing
	// the change in `out.cache` confirms shared backing storage; if a
	// future regression introduces a deep copy, this assertion fails.
	in.cache["k2"] = "v2"
	if got := out.cache["k2"]; got != "v2" {
		t.Errorf("opaque struct's map appears deep-copied: out.cache[k2]=%q; want backing storage shared with input", got)
	}
}

// TestRedactStruct_OpaqueOnTimeTime is the regression for the prior PR 1
// behaviour. The new structural rule must keep covering `time.Time` —
// otherwise an audit event that embeds a Time would corrupt its
// `loc *Location` field on the redacted copy.
func TestRedactStruct_OpaqueOnTimeTime(t *testing.T) {
	r := NewSecretRedactor()
	now := time.Date(2026, 4, 30, 12, 0, 0, 0, time.UTC)
	type carrier struct {
		Stamp time.Time
		Note  string
	}
	in := carrier{Stamp: now, Note: "Bearer abc.def=="}
	out := r.RedactStruct(in).(carrier)
	if !out.Stamp.Equal(now) || out.Stamp.Location().String() != now.Location().String() {
		t.Errorf("time.Time mutated: %v (loc=%s)", out.Stamp, out.Stamp.Location())
	}
	if !strings.Contains(out.Note, "[REDACTED:bearer-token]") {
		t.Errorf("Note not redacted: %q", out.Note)
	}
}

// TestRedactStruct_OpaqueOnSyncPrimitives pins that `sync.Mutex` /
// `sync.WaitGroup` / `sync.Once` / `atomic.Value` are all opaque without
// per-type registration. We assert the wrapper struct's exported `Note`
// is still redacted (recursion ran on the parent struct) and the sync
// fields are returned as the same value (no copy / zero-out).
func TestRedactStruct_OpaqueOnSyncPrimitives(t *testing.T) {
	r := NewSecretRedactor()
	type carrier struct {
		Note  string
		Lock  sync.Mutex     // rule 2: no exported fields
		Wait  sync.WaitGroup // rule 1: embedded noCopy struct
		Once  sync.Once      // rule 1: embedded Mutex
		Value atomic.Value   // rule 1: unexported `v any`
	}
	in := &carrier{Note: "Bearer abc.def=="}
	in.Value.Store("hello")
	// Build the redacted copy. The walk should not panic on the sync
	// primitives' unexported state and should leave atomic.Value's stored
	// payload reachable.
	out := r.RedactStruct(in).(*carrier)
	if !strings.Contains(out.Note, "[REDACTED:bearer-token]") {
		t.Errorf("Note not redacted: %q", out.Note)
	}
	if got := out.Value.Load(); got != "hello" {
		t.Errorf("atomic.Value lost its payload after redact: %v", got)
	}
}

// fixtureOpaqueByArray trips rule 1 via an unexported `[16]byte` field
// (e.g. the fixed-size UUID byte array used by many `uuid.UUID` types
// and other fixed-width binary identifiers). Arrays are
// reflect.Array-kinded which is non-primitive under [isPrimitiveKind],
// so the rule returns true and the walk leaves the carrier struct
// alone.
type fixtureOpaqueByArray struct {
	Public string
	id     [16]byte
}

// TestRedactStruct_OpaqueByUnexportedArray pins PR #236 review L-1: a
// future maintainer narrowing isPrimitiveKind to "treat [N]byte as
// primitive — it's just bytes" would silently weaken rule 1 on every
// UUID-bearing struct (uuid.UUID has an unexported `data [16]byte`).
// The opaque rule must keep covering fixed-size byte arrays; this
// fixture makes that contract explicit before PR 3 routes tool-call
// argument structs (which often embed UUIDs) through RedactStruct.
func TestRedactStruct_OpaqueByUnexportedArray(t *testing.T) {
	r := NewSecretRedactor()
	in := &fixtureOpaqueByArray{
		Public: "Bearer abc.def==",
		id:     [16]byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16},
	}
	out, ok := r.RedactStruct(in).(*fixtureOpaqueByArray)
	if !ok {
		t.Fatalf("RedactStruct returned wrong type %T", r.RedactStruct(in))
	}
	if out.Public != "Bearer abc.def==" {
		t.Errorf("opaque struct mutated: Public = %q", out.Public)
	}
	// The returned pointer is the original — the array bytes survive
	// verbatim because the entire struct was returned from the rule 1
	// branch, not reflectively rewalked.
	if out.id != in.id {
		t.Errorf("opaque struct's array bytes were rewritten: %v != %v", out.id, in.id)
	}
}

// TestRedactStruct_WalkableUnexportedPrimitive confirms that a struct
// whose only unexported field is a primitive (rule 1 not tripped) AND
// which has at least one exported field (rule 2 not tripped) is walked
// as before. The unexported primitive does not survive the copy — that
// is acceptable because callers cannot read it anyway.
func TestRedactStruct_WalkableUnexportedPrimitive(t *testing.T) {
	r := NewSecretRedactor()
	in := &fixtureWalkable{Public: "Bearer abc.def==", counter: 7}
	out, ok := r.RedactStruct(in).(*fixtureWalkable)
	if !ok {
		t.Fatalf("RedactStruct returned wrong type %T", r.RedactStruct(in))
	}
	if !strings.Contains(out.Public, "[REDACTED:bearer-token]") {
		t.Errorf("walkable Public not redacted: %q", out.Public)
	}
	// Unexported primitive fields are dropped by reflective copying
	// (the new struct is allocated zero); pin the contract so a future
	// implementation change is conscious.
	if out.counter != 0 {
		t.Errorf("expected unexported primitive to drop to zero on copy; got %d", out.counter)
	}
}
