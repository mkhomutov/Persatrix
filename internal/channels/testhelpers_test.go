package channels

import "os"

// writeFile is a tiny helper kept in a non-test file so config_test.go and
// other test files can share it without import cycles.
func writeFile(path, body string) error {
	return os.WriteFile(path, []byte(body), 0o600)
}
