package logbuffer

import "os"

func mkdirAll(path string) error { return os.MkdirAll(path, 0o700) }

func writeFile(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o600)
}

func stat(path string) (os.FileInfo, error) { return os.Stat(path) }
