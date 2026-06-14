---
id: ISSUE-0104
summary: "The orchestrator Docker image fails to build on linux/arm64 (Apple Silicon): Dockerfile.orchestrator's Stage-1 ui-builder runs `npm ci && npm run build`, which dies with `Cannot find module @rollup/rollup-linux-arm64-musl` (npm optional-dependencies bug, npm/cli#4828). This breaks `docker compose up --build`, `make demo-anthropic`, and any live manual-test arc that needs a fresh image on arm64; likely fallout of the #644 vite/esbuild bump's lockfile."
status: open
severity: medium
area: build/docker
created: 2026-06-14
refs:
  - docs/manual-tests/MT-CHANNEL-CONFIG-001.md
---

## Summary

`docker compose ... up --build` for the orchestrator **fails on linux/arm64**.
The web-console bundle stage cannot resolve rollup's platform-specific native
module, so the image never builds — blocking the canonical
`make demo-*` / `docker compose up --build` path and every live manual test that
needs a rebuilt image on Apple Silicon.

## Context

Found while bringing up the fleet for the
[MT-CHANNEL-CONFIG-001](../manual-tests/MT-CHANNEL-CONFIG-001.md) first live run
(2026-06-14). [`Dockerfile.orchestrator`](../../Dockerfile.orchestrator) Stage 1
(`ui-builder`, `node:22-alpine`) runs:

```dockerfile
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci
COPY web/ ./
RUN npm run build      # ← fails
```

with:

```
Error: Cannot find module @rollup/rollup-linux-arm64-musl.
npm has a bug related to optional dependencies (https://github.com/npm/cli/issues/4828).
```

`npm ci` does not install the `linux-arm64-musl` rollup optional dependency, so
`vite build` (rollup) cannot start. This is the well-known npm optional-deps bug
and almost certainly surfaced with the #644 bump of `vite` / `esbuild` /
`@sveltejs/vite-plugin-svelte` (the lockfile state changed). The host build is
fine — `cd web && npm run build` succeeds on darwin/arm64 — because the bug is
about which platform's native module `npm ci` materializes, not the bundle
output (the emitted JS/CSS is platform-agnostic).

## Impact

- `docker compose up --build` / `make demo-anthropic` / `make demo-*` do not
  work on arm64 from a clean state — the documented one-step bring-up is broken
  on the most common dev hardware.
- Live manual-test arcs that depend on a current image (most do — the prebuilt
  `persatrix-orchestrator:latest` and host `bin/persatrix` are frequently stale)
  cannot run without a manual workaround.
- CI is presumably x86_64 and unaffected, so the break is invisible to the
  pipeline and only bites local arm64 operators.

## Proposed fix / investigation path

Several known-good options, cheapest first:

1. **Make the install resilient in the Dockerfile** — e.g.
   `RUN npm ci || (rm -rf node_modules package-lock.json && npm install)`, or
   explicitly `npm install @rollup/rollup-linux-arm64-musl --no-save` after
   `npm ci`. Quick, unblocks arm64, but the fallback can drift from the lockfile.
2. **Repair `web/package-lock.json`** so it carries the full set of rollup
   optional dependencies for all target platforms (regenerate on a clean
   `npm install` and commit) — the proper fix if #644 truncated it.
3. **Pin/upgrade npm** in the `node:22-alpine` stage to a version past the
   optional-deps bug, if one resolves it cleanly.

Verify the chosen fix with a clean `docker compose -f docker-compose.yaml -f
docker-compose.anthropic.yaml build orchestrator` on arm64.

## Notes

> 2026-06-14 — captured during the MT-CHANNEL-CONFIG-001 first live run.
> Workaround used for that run: build the web bundle on the host
> (`cd web && npm run build`), then build the orchestrator from a throwaway
> Dockerfile that skips the ui-builder stage and bakes the host-built
> `internal/ui/assets` from the build context, tagged
> `persatrix-orchestrator:latest`, and `docker compose up -d` WITHOUT `--build`.
> This is a stopgap, not a fix.
