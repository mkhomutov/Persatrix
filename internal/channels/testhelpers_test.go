package channels

import "os"

// writeFile is a tiny helper shared across the channels test suite. The
// `_test.go` suffix scopes it to test builds; placing it in its own file
// (rather than inside one of the *_test.go files that uses it) avoids a
// compile error when only a subset of tests are built with `go test -run`.
func writeFile(path, body string) error {
	return os.WriteFile(path, []byte(body), 0o600)
}
