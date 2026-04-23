package logbuffer

import (
	"os"
	"time"
)

func mkdirAll(path string) error { return os.MkdirAll(path, 0o700) }

func writeFile(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o600)
}

func stat(path string) (os.FileInfo, error) { return os.Stat(path) }

// chTimes sets atime+mtime on path; used by warm-load tests that need
// deterministic disk.list() ordering.
func chTimes(path string, t time.Time) error { return os.Chtimes(path, t, t) }
