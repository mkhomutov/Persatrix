package ui

import (
	"io/fs"
	"testing"
)

// TestAssets_TreeContainsOnlyWebAssets is the contract for the embedded
// console tree (RFC 0048 Phase 1 PR 1, review finding #1): //go:embed
// all:assets must surface only files that are meant to be served at the web
// root. In particular the bundle-ignore rules live in the repo-root .gitignore
// (not a co-located assets/.gitignore), so no VCS file is embedded and served
// at /ui/.gitignore.
//
// The `all:` prefix is still required (PR 3's Vite emits dot-prefixed entries
// like .vite/manifest.json); this test guards that keeping `all:` does not also
// drag a tracked .gitignore into the served tree.
func TestAssets_TreeContainsOnlyWebAssets(t *testing.T) {
	got := map[string]bool{}
	if err := fs.WalkDir(Assets(), ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		got[p] = true
		return nil
	}); err != nil {
		t.Fatalf("walking embedded assets: %v", err)
	}

	if got[".gitignore"] {
		t.Errorf("embedded tree serves .gitignore at /ui/.gitignore; keep bundle-ignore rules in the repo-root .gitignore so assets/ embeds only web assets")
	}
	if !got["index.html"] {
		t.Errorf("embedded tree must contain the placeholder index.html; got %v", got)
	}
}
