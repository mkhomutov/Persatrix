# Web console (`web/`)

The embedded operator/tester web console — **RFC 0048**, Phase 1 / Slice 1. A
plain [Svelte 5](https://svelte.dev) + [Vite](https://vitejs.dev) single-page
app that the Go orchestrator serves **same-origin** under `/ui` when started
with `--enable-ui` (off by default). It boots off two read-only endpoints,
`GET /api/v1/ui/config` (which panels to show) and `GET /api/v1/ui/context`
(who the principal is), and renders only the panels that are both *enabled*
(operator toggle in `config/ui.yaml`) and *available* (subsystem wired).

This directory is the repo's only JavaScript toolchain; it is isolated here so
Go-only contributors are never required to install Node (see *Go-only contributors* below).

## Layout

```
web/
  src/
    main.js                 # mounts the app
    App.svelte              # shell: boot, tabs, active panel, error states
    app.css                 # minimal styling
    lib/
      bootstrap.js          # pure panel-selection + identity logic (RFC §C/§F)
      api.js                # same-origin fetch client (config/context today)
    panels/
      Chat.svelte           # slot — real panel lands in PR 4
      ChannelTimeline.svelte# slot — real panel lands in PR 5
  *.test.js                 # Vitest unit/component tests, co-located
```

## Develop

```bash
npm install        # first time (or `npm ci` for an exact lockfile install)
npm run dev        # Vite dev server with HMR
npm test           # Vitest (unit + component, run-once)
npm run test:watch # Vitest in watch mode
```

The dev server runs the SPA standalone. Because `vite.config.js` sets
`base: "/ui/"` (the subtree the orchestrator serves under), the dev server also
mounts the app there — open `http://localhost:5173/ui/`, not the bare root. To
exercise it against the real backend, run the orchestrator with `--enable-ui`
and open `http://localhost:8080/ui`.

## Build (what ships)

`npm run build` (or, from the repo root, **`make ui`**) compiles the SPA into
`../internal/ui/assets/`, the `//go:embed` tree the orchestrator serves. The
build output is **git-ignored** — only the committed placeholder `index.html`
and this `web/` source are tracked, so the generated hashed bundle is never
committed (see the heads-up below for the one tracked file the build rewrites).
`vite.config.js` sets `base: "/ui/"` so asset URLs resolve under the `/ui/`
subtree.

To build a binary with the real console embedded:

```bash
make ui                       # build the bundle into internal/ui/assets/
make build-orchestrator       # embed it
# or, in one step:
make build-orchestrator-ui
```

> **Heads-up — `make ui` dirties your working tree.** The hashed JS/CSS are
> git-ignored, but `index.html` is the one *tracked* path the build also
> overwrites (Vite emits its own `index.html` pointing at the hashed bundle).
> So after `make ui`, `git status` shows `internal/ui/assets/index.html` as
> modified — that diff is build output and **must not be committed**: a
> committed build-output `index.html` references hashes that aren't in git, so a
> clean checkout 404s every console asset. Restore the placeholder when you're
> done:
>
> ```bash
> git checkout -- internal/ui/assets/index.html
> ```
>
> The `go` CI lane guards this — it fails if the checked-in `index.html` is no
> longer the placeholder.

## Go-only contributors

You do **not** need Node to build or test the orchestrator. `go build ./...`
and the Go test suite compile against the committed placeholder
`internal/ui/assets/index.html`; only the release/asset CI lane (and
`make ui` / `make build-orchestrator-ui`) runs the JS toolchain.
