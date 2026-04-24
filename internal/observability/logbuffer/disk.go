package logbuffer

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	"go.uber.org/zap"
)

// diskStore implements the on-disk durability layer described in the
// PR plan: per-execution directories under root, one append-only JSONL
// file per sealed flush named <sequence>.jsonl. The flat-file layout
// from RFC § E is intentionally superseded — see PR plan note.
type diskStore struct {
	root     string
	cap      int64
	logger   *zap.Logger
	mu       sync.Mutex
	usage    atomic.Int64
	nextSeq  map[string]int // execution_id → next sequence number
	totalMap map[string]int64
}

func newDiskStore(root string, capBytes int64, logger *zap.Logger) (*diskStore, error) {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, fmt.Errorf("logbuffer: create dir %q: %w", root, err)
	}
	d := &diskStore{
		root:     root,
		cap:      capBytes,
		logger:   logger,
		nextSeq:  make(map[string]int),
		totalMap: make(map[string]int64),
	}
	if err := d.scan(); err != nil {
		return nil, err
	}
	return d, nil
}

// scan walks root to populate nextSeq, current usage, and per-execution
// byte totals. Called once at construction.
func (d *diskStore) scan() error {
	entries, err := os.ReadDir(d.root)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("logbuffer: scan %q: %w", d.root, err)
	}
	var total int64
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		id := e.Name()
		dir := filepath.Join(d.root, id)
		files, err := os.ReadDir(dir)
		if err != nil {
			d.logger.Warn("logbuffer scan: skip unreadable execution dir",
				zap.String("execution_id", id), zap.Error(err))
			continue
		}
		var maxSeq int
		var bytes int64
		for _, f := range files {
			if f.IsDir() || !strings.HasSuffix(f.Name(), ".jsonl") {
				continue
			}
			seqStr := strings.TrimSuffix(f.Name(), ".jsonl")
			// strconv.Atoi rejects whitespace and signed integers
			// deterministically; the previous fmt.Sscanf("%d", ...)
			// silently accepted both. Pair with seq < 1 to skip
			// externally-dropped junk filenames (PR #172 review nit).
			seq, err := strconv.Atoi(seqStr)
			if err != nil || seq < 1 {
				continue
			}
			if seq > maxSeq {
				maxSeq = seq
			}
			info, err := f.Info()
			if err == nil {
				bytes += info.Size()
			}
		}
		d.nextSeq[id] = maxSeq + 1
		d.totalMap[id] = bytes
		total += bytes
	}
	d.usage.Store(total)
	return nil
}

// list returns the execution IDs known on disk, sorted by directory
// modtime (oldest first) so warm-load preserves chronological order.
func (d *diskStore) list() ([]string, error) {
	entries, err := os.ReadDir(d.root)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("logbuffer: list %q: %w", d.root, err)
	}
	type item struct {
		id   string
		mtim int64
	}
	items := make([]item, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		items = append(items, item{id: e.Name(), mtim: info.ModTime().UnixNano()})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].mtim < items[j].mtim })
	out := make([]string, len(items))
	for i, it := range items {
		out[i] = it.id
	}
	return out, nil
}

// flush writes all entries for executionID to a single new sequence
// file, atomically. After write succeeds, the byte count is added to
// usage and an eviction sweep is triggered if the disk cap is exceeded.
//
// executionID is re-validated here as defence-in-depth even though
// Buffer.Append/Seal already gate on validExecutionID — diskStore is
// package-private but multiple call sites construct paths from
// executionID, and a regression in any one of them would otherwise
// silently re-open the path-traversal hole.
//
// Critical-section shape (PR #172 review Should-Fix #2): d.mu is held
// only long enough to reserve the next sequence number for this
// execution. The actual file IO (mkdir, open, write, fsync, parent
// fsync, rename) and the post-write totalMap / usage update happen
// without the lock so concurrent flushes for *different* executions
// no longer serialise on a single fsync. The follow-up evictIfOverCap
// re-acquires d.mu only for the bookkeeping snapshot; per-victim
// RemoveAll runs unlocked.
func (d *diskStore) flush(executionID string, entries []Entry) error {
	if len(entries) == 0 {
		return nil
	}
	if !validExecutionID(executionID) {
		return fmt.Errorf("logbuffer: refusing flush with invalid execution_id %q", executionID)
	}

	dir := filepath.Join(d.root, executionID)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("logbuffer: mkdir %q: %w", dir, err)
	}

	// Reserve the next sequence under the short critical section.
	d.mu.Lock()
	seq := d.nextSeq[executionID]
	if seq == 0 {
		seq = 1
	}
	d.nextSeq[executionID] = seq + 1
	d.mu.Unlock()

	path := filepath.Join(dir, fmt.Sprintf("%010d.jsonl", seq))
	tmp := path + ".tmp"

	// O_NOFOLLOW (POSIX) refuses to open a symlinked target so a
	// same-UID local actor pre-creating <DIR>/<exec_id>/<seq>.jsonl.tmp
	// as a symlink to e.g. ~/.ssh/authorized_keys cannot have that
	// target truncated by the O_TRUNC below. The flag is benignly
	// ignored on Windows (constant resolves to 0 there) and on
	// platforms where the kernel does not support it.
	f, err := os.OpenFile(tmp, openFlagsNoFollow, 0o600)
	if err != nil {
		return fmt.Errorf("logbuffer: open %q: %w", tmp, err)
	}
	bw := bufio.NewWriter(f)
	enc := json.NewEncoder(bw)
	for i := range entries {
		if err := enc.Encode(&entries[i]); err != nil {
			_ = f.Close()
			_ = os.Remove(tmp)
			return fmt.Errorf("logbuffer: encode entry: %w", err)
		}
	}
	if err := bw.Flush(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("logbuffer: flush %q: %w", tmp, err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("logbuffer: sync %q: %w", tmp, err)
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("logbuffer: close %q: %w", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("logbuffer: rename %q: %w", path, err)
	}
	// fsync the parent directory so the rename's metadata survives a
	// crash. Without this the file contents are durable but the
	// directory entry pointing at them can disappear on POSIX
	// filesystems, defeating the on-disk durability claim. Errors are
	// logged but do not fail the flush — the data is on disk; only
	// the durability *guarantee* is weakened. Best-effort on Windows
	// where directory fsync is a no-op.
	if dirF, derr := os.Open(dir); derr == nil {
		if serr := dirF.Sync(); serr != nil {
			d.logger.Warn("logbuffer: parent dir fsync failed",
				zap.String("dir", dir), zap.Error(serr))
		}
		_ = dirF.Close()
	}

	var added int64
	if info, statErr := os.Stat(path); statErr == nil {
		added = info.Size()
	}
	d.mu.Lock()
	d.totalMap[executionID] += added
	d.mu.Unlock()
	d.usage.Add(added)

	d.evictIfOverCap()
	return nil
}

// read returns all entries for executionID currently on disk, in
// chronological order across sequence files. Truncated final lines
// (e.g. from a crash mid-write — should not happen given the atomic
// rename on flush, but warm-load resilience matters across formats)
// are tolerated: the well-formed prefix is returned and a single WARN
// is logged per affected file.
//
// As in flush, executionID is re-validated locally so a buggy caller
// cannot use this entry-point as a path-traversal oracle (`Snapshot`
// already validates, but disk.read is reachable directly from
// warmLoad and from any future internal caller).
func (d *diskStore) read(executionID string) []Entry {
	if !validExecutionID(executionID) {
		return nil
	}
	dir := filepath.Join(d.root, executionID)
	files, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	type seqFile struct {
		seq  int
		path string
	}
	var sorted []seqFile
	for _, f := range files {
		if f.IsDir() || !strings.HasSuffix(f.Name(), ".jsonl") {
			continue
		}
		seqStr := strings.TrimSuffix(f.Name(), ".jsonl")
		seq, err := strconv.Atoi(seqStr)
		if err != nil || seq < 1 {
			continue
		}
		sorted = append(sorted, seqFile{seq: seq, path: filepath.Join(dir, f.Name())})
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].seq < sorted[j].seq })

	var out []Entry
	for _, sf := range sorted {
		f, err := os.Open(sf.path)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		var malformed bool
		for scanner.Scan() {
			var e Entry
			if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
				malformed = true
				continue
			}
			out = append(out, e)
		}
		if err := scanner.Err(); err != nil {
			malformed = true
		}
		_ = f.Close()
		if malformed {
			d.logger.Warn("logbuffer: skipped malformed line(s) in sequence file",
				zap.String("path", sf.path))
		}
	}
	return out
}

// evictIfOverCap deletes oldest-first execution directories until usage
// is back under cap.
//
// Snapshot the candidate set under d.mu (so concurrent flush() calls
// see a consistent totalMap), then perform os.RemoveAll outside the
// lock so concurrent flushes for unrelated executions are not blocked
// on a slow recursive delete (PR #172 review Should-Fix #2).
//
// mtimes are read from the on-disk directory once per sweep rather
// than maintained in totalMap because the eviction order has a small
// failure tolerance (one extra eviction is benign) and a missing
// mtime simply demotes the candidate to "stale enough to evict".
func (d *diskStore) evictIfOverCap() {
	if d.cap <= 0 || d.usage.Load() <= d.cap {
		return
	}
	d.mu.Lock()
	type item struct {
		id   string
		size int64
		mtim int64
	}
	candidates := make([]item, 0, len(d.totalMap))
	for id, size := range d.totalMap {
		var mtim int64
		if info, err := os.Stat(filepath.Join(d.root, id)); err == nil {
			mtim = info.ModTime().UnixNano()
		}
		candidates = append(candidates, item{id: id, size: size, mtim: mtim})
	}
	d.mu.Unlock()

	sort.Slice(candidates, func(i, j int) bool { return candidates[i].mtim < candidates[j].mtim })
	for _, it := range candidates {
		if d.usage.Load() <= d.cap {
			return
		}
		dir := filepath.Join(d.root, it.id)
		if err := os.RemoveAll(dir); err != nil {
			d.logger.Warn("logbuffer: failed to evict",
				zap.String("execution_id", it.id), zap.Error(err))
			continue
		}
		// Re-read totalMap[it.id] under the lock and subtract that, not the
		// snapshot size. A concurrent flush() between snapshot and RemoveAll
		// may have grown totalMap[it.id] by delta bytes; RemoveAll wiped
		// everything on disk (snapshot + delta), so subtracting only the
		// snapshot would leak delta into d.usage permanently (it never
		// self-heals because the map entry is then deleted). Subtracting the
		// final map value matches what was actually removed from disk.
		// PR #177 review Should-Fix #2.
		d.mu.Lock()
		finalSize, ok := d.totalMap[it.id]
		if !ok {
			finalSize = it.size
		}
		delete(d.totalMap, it.id)
		delete(d.nextSeq, it.id)
		d.mu.Unlock()
		d.usage.Add(-finalSize)
		d.logger.Info("logbuffer: evicted execution from disk",
			zap.String("execution_id", it.id), zap.Int64("freed_bytes", finalSize))
	}
}
