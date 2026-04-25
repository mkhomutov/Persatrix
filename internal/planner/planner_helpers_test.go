package planner

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// Compile-time check: YAMLPlanner implements Planner.
var _ Planner = (*YAMLPlanner)(nil)

func newTestPlanner() *YAMLPlanner {
	return NewYAMLPlanner(zap.NewNop())
}

func writeTempYAML(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "test.yaml")
	err := os.WriteFile(path, []byte(content), 0o644)
	require.NoError(t, err)
	return path
}
