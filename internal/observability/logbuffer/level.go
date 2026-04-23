package logbuffer

import "strings"

// Level rankings for the drop-level filter and for the always-admit
// rule on WARN/ERROR. Unknown levels are treated as INFO to fail open
// rather than silently swallow misconfigured emitters.
var levelRank = map[string]int{
	"DEBUG": 10,
	"INFO":  20,
	"WARN":  30,
	"ERROR": 40,
	"FATAL": 50,
}

// levelGE reports whether actual ≥ threshold.
func levelGE(actual, threshold string) bool {
	a, ok := levelRank[strings.ToUpper(actual)]
	if !ok {
		a = levelRank["INFO"]
	}
	t, ok := levelRank[strings.ToUpper(threshold)]
	if !ok {
		t = levelRank["DEBUG"]
	}
	return a >= t
}
