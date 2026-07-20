import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// RFC 0048 Phase 1 PR 3 — the embedded web console's build config.
//
// `base: "/ui/"` is load-bearing: the orchestrator serves these assets under
// the /ui/ subtree (server.WithUI), so the built index.html must reference its
// JS/CSS as /ui/<asset> rather than the root-absolute /<asset> Vite emits by
// default — otherwise every sub-resource 404s same-origin. See the PR plan's
// D1/PR-3 "base is load-bearing" note.
//
// The build writes into internal/ui/assets/ (the //go:embed tree), overwriting
// the committed placeholder index.html. `emptyOutDir: true` is required because
// the dir lives outside the Vite root (web/); without it Vite refuses to clear
// an out-of-root directory and would leave stale hashed assets behind. The
// generated bundle is gitignored (only the placeholder is tracked), so a clean
// checkout always has just the placeholder and a release build has just the
// real bundle — they never coexist in git (PR plan D2).
export default defineConfig(({ mode }) => ({
  plugins: [svelte()],
  base: "/ui/",
  // Dev-only API proxy: the SPA's fetch paths are root-relative (/api/v1/…)
  // because the deployed bundle is same-origin under the orchestrator. Under
  // `npm run dev` there is no orchestrator on the Vite origin, so proxy /api to
  // one running locally (default :8080; override with VITE_API_TARGET, e.g. a
  // mock server). Ignored entirely by `vite build`.
  server: {
    proxy: {
      "/api": process.env.VITE_API_TARGET ?? "http://localhost:8080",
    },
  },
  build: {
    outDir: "../internal/ui/assets",
    emptyOutDir: true,
  },
  // Under Vitest, resolve Svelte's browser (client) build, not its SSR build:
  // @testing-library/svelte mounts components in jsdom and Svelte 5's default
  // export condition would otherwise pick the server runtime, whose mount() is
  // unavailable. Scoped to test mode so the production bundle is unchanged.
  ...(mode === "test" ? { resolve: { conditions: ["browser"] } } : {}),
  test: {
    environment: "jsdom",
    include: ["src/**/*.{test,spec}.js"],
  },
}));
