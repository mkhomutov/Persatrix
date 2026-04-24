//go:build !unix

package logbuffer

import "os"

// openFlagsNoFollow on non-unix targets falls back to the original
// flag set. NTFS / Windows symlinks require SE_CREATE_SYMBOLIC_LINK
// privilege by default, so the same-UID symlink-attack vector
// motivating O_NOFOLLOW on POSIX does not apply in the same shape.
const openFlagsNoFollow = os.O_CREATE | os.O_WRONLY | os.O_TRUNC
