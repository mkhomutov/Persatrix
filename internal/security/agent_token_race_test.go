//go:build race

package security

// A build with the race detector sets raceDetector, which changes what some
// tests can measure.
func init() { raceDetector = true }
