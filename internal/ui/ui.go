// Package ui embeds the operator/tester web console's static assets and
// exposes them as an fs.FS for the orchestrator to serve under /ui/
// (RFC 0048 — Operator & Tester Web Console, Phase 1 / Slice 1).
//
// The embedded tree is produced by the Svelte/Vite build (`make ui`, wired in
// RFC 0048 Phase 1 PR 3), which writes hashed JS/CSS bundles into the assets/
// directory. To keep `go build ./...` green for Go-only contributors and the
// Go-only CI lane — neither of which runs the JS toolchain — a minimal
// placeholder index.html is committed at assets/index.html so the //go:embed
// directive always has something to compile against. A release build overwrites
// it with the real bundle before embedding; the committed placeholder and the
// generated bundle never coexist in a release artifact (see the PR plan's D2).
package ui

import (
	"embed"
	"io/fs"
)

// embeddedAssets holds the console's static asset tree rooted at assets/.
//
// The `all:` prefix is deliberate: Vite emits dot-prefixed entries (e.g.
// .vite/manifest.json) in some configurations, which a bare //go:embed would
// silently drop. `all:` includes them so PR 3's build needs no embed-directive
// change.
//
//go:embed all:assets
var embeddedAssets embed.FS

// Assets returns the console's static asset tree with the assets/ prefix
// stripped, so callers serve files at their web-root paths (index.html, not
// assets/index.html). Wire it into the server via server.WithUI(ui.Assets()).
//
// The fs.Sub error is impossible at runtime: "assets" is a constant, valid,
// embedded path. Panicking on it surfaces a build/embed regression loudly at
// startup rather than serving an empty tree that 404s every asset.
func Assets() fs.FS {
	sub, err := fs.Sub(embeddedAssets, "assets")
	if err != nil {
		panic("ui: embedded assets subtree missing — build regression: " + err.Error())
	}
	return sub
}
