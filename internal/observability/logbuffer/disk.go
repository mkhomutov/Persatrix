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
			var seq int
			if _, err := fmt.Sscanf(seqStr, "%d", &seq); err != nil {
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
func (d *diskStore) flush(executionID string, entries []Entry) error {
	if len(entries) == 0 {
		return nil
	}
	if !validExecutionID(executionID) {
		return fmt.Errorf("logbuffer: refusing flush with invalid execution_id %q", executionID)
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	dir := filepath.Join(d.root, executionID)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("logbuffer: mkdir %q: %w", dir, err)
	}
	seq := d.nextSeq[executionID]
	if seq == 0 {
		seq = 1
	}
	path := filepath.Join(dir, fmt.Sprintf("%010d.jsonl", seq))
	tmp := path + ".tmp"

	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
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
	info, err := os.Stat(path)
	if err == nil {
		d.totalMap[executionID] += info.Size()
		d.usage.Add(info.Size())
	}
	d.nextSeq[executionID] = seq + 1

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
		var seq int
		if _, err := fmt.Sscanf(seqStr, "%d", &seq); err != nil {
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
// is back under cap. Caller holds d.mu.
func (d *diskStore) evictIfOverCap() {
	if d.cap <= 0 || d.usage.Load() <= d.cap {
		return
	}
	entries, err := os.ReadDir(d.root)
	if err != nil {
		return
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
	for _, it := range items {
		if d.usage.Load() <= d.cap {
			return
		}
		dir := filepath.Join(d.root, it.id)
		size := d.totalMap[it.id]
		if err := os.RemoveAll(dir); err != nil {
			d.logger.Warn("logbuffer: failed to evict",
				zap.String("execution_id", it.id), zap.Error(err))
			continue
		}
		d.usage.Add(-size)
		delete(d.totalMap, it.id)
		delete(d.nextSeq, it.id)
		d.logger.Info("logbuffer: evicted execution from disk",
			zap.String("execution_id", it.id), zap.Int64("freed_bytes", size))
	}
}
