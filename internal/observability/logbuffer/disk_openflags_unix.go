//go:build unix

package logbuffer

import (
	"os"
	"syscall"
)

// openFlagsNoFollow is the flag set used by diskStore.flush when
// opening the per-sequence .jsonl.tmp file. O_NOFOLLOW refuses to
// open a symlinked target, blocking a same-UID local actor from
// pre-creating the path as a symlink to e.g. ~/.ssh/authorized_keys
// (the O_TRUNC below would otherwise truncate the symlink target).
//
// See PR #172 review: 0700 dir mode mitigates cross-user attack but
// not the same-UID variant; this is defence-in-depth.
const openFlagsNoFollow = os.O_CREATE | os.O_WRONLY | os.O_TRUNC | syscall.O_NOFOLLOW
