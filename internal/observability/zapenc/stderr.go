package zapenc

import (
	"io"
	"os"
)

// newStderr returns the default sink for redactor-panic fallback warnings.
// Split out so encoder_test can swap in an in-memory writer without touching
// the OS-level os.Stderr file descriptor.
func newStderr() io.Writer {
	return os.Stderr
}
