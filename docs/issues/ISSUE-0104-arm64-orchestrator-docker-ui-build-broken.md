---
id: ISSUE-0104
summary: "The orchestrator Docker image failed to build on linux/arm64 (Apple Silicon) with `Cannot find module @rollup/rollup-linux-arm64-musl`. ROOT CAUSE (not the npm optional-deps bug, and not the lockfile): `.dockerignore` listed a root-anchored `node_modules/`, which does NOT match the nested `web/node_modules`, so `COPY web/ ./` in Dockerfile.orchestrator leaked the host's macOS `web/node_modules` into the image and clobbered the clean in-image `npm ci`. The leaked host tree carried rollup with only the darwin binding, so `vite build` died on the missing linux-arm64-musl one. FIX: `.dockerignore` `node_modules/` -> `**/node_modules/`. The lockfile was already correct (vite 8 -> rolldown, full bindings; a clean `npm ci` builds fine)."
status: resolved
severity: medium
area: build/docker
created: 2026-06-14
closed: 2026-06-15
closed_pr: 650
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

## Root cause (corrected 2026-06-15)

The original diagnosis above (npm optional-deps bug / truncated lockfile from
the #644 vite bump) was **wrong**. Verified ground truth:

- #644 bumped `vite` to `^8.0.16`, and **vite 8 bundles with rolldown, not
  rollup**. The committed `web/package-lock.json` carries **zero** rollup entries
  and the full rolldown binding set, including `@rolldown/binding-linux-arm64-musl@1.0.3`.
- A clean `npm ci && npm run build` in a `node:22-alpine` **arm64** container
  **succeeds** (~378 ms, emits the hashed bundle). The lockfile is fine.
- The real culprit is `Dockerfile.orchestrator` Stage 1:
  ```dockerfile
  RUN npm ci          # clean, correct, rolldown-based install
  COPY web/ ./        # ← re-copies the host's web/ INCLUDING web/node_modules
  RUN npm run build   # now loads the leaked host node_modules and dies
  ```
  `.dockerignore` listed a **root-anchored** `node_modules/`, which does **not**
  match the nested `web/node_modules`. So `COPY web/ ./` overwrote the clean
  in-image install with the operator's macOS `web/node_modules` — a stale tree
  (often left by the `cd web && npm run build` host workaround) that still
  carries **rollup** with only the *darwin* native binding. `vite build` then
  loaded that rollup and failed on the missing `@rollup/rollup-linux-arm64-musl`.
- Proven with a real `docker build` honouring the repo `.dockerignore`:
  `web/node_modules` reached the image (`/w/node_modules/rollup/dist/native.js`
  present). This is why it bit local arm64 operators and was invisible to
  clean-x86 CI — CI has no host `web/node_modules` to leak.

## Resolution

`.dockerignore`: `node_modules/` → `**/node_modules/` (the `**/` is required to
match nested dirs). This stops the host tree from leaking, restoring the
Dockerfile's stated design goal of a host-state-independent, self-contained
image. No lockfile change, no `npm ci` fallback, no npm pin — those would have
papered over a build-context-hygiene bug.

Verified on darwin/arm64:

- Leak probe with the fix → `clean: no node_modules in context`.
- `docker build --no-cache -f Dockerfile.orchestrator` → full image builds; the
  embedded bundle hash (`index-HARX7q1C.js`) matches the clean rolldown build,
  confirming the real console is baked in (not the placeholder).

Regression guard: the `dockerignore-hygiene` CI job (`make dockerignore-check`,
`scripts/checks/dockerignore_context.py`) seeds a sentinel under
`web/node_modules` and runs a real `docker build` to assert the nested tree does
NOT reach the build context. It seeds the sentinel because a clean checkout has
none to leak — the exact reason this break was invisible to CI — and uses real
Docker because Docker anchors `node_modules/` to the context root (a string or
gitignore check would wrongly call the buggy pattern safe).

## Notes

> 2026-06-14 — captured during the MT-CHANNEL-CONFIG-001 first live run.
> Workaround used for that run: build the web bundle on the host
> (`cd web && npm run build`), then build the orchestrator from a throwaway
> Dockerfile that skips the ui-builder stage and bakes the host-built
> `internal/ui/assets` from the build context, tagged
> `persatrix-orchestrator:latest`, and `docker compose up -d` WITHOUT `--build`.
> This was a stopgap; the `.dockerignore` fix removes the need for it.
