---
id: ISSUE-0087
summary: "The RFC 0048 web console introduced the repo's first npm toolchain, and Dependabot flags three advisories against `web/package-lock.json`: vitest < 4.1.0 (GHSA-5xrq-8626-4rwp, nominally **critical** — arbitrary file read/exec via the Vitest UI server), vite <= 6.4.1 (GHSA-4w7w-66w2-5vf9, medium — optimized-deps `.map` path traversal), and esbuild <= 0.24.2 (GHSA-67mh-4wv8-2f99, medium — dev server accepts cross-origin requests). All three are **dev/build-time only** and none ships in the deployed artifact (a static prebuilt bundle + the Go binary), and the vitest `critical` exploit path requires the Vitest UI server — which this project never runs (`vitest run` is headless; there is no `@vitest/ui` dependency). Track the dependency bumps here; the vitest fix is a 2.x → 4.x major that needs a full suite re-validation."
status: open
severity: medium
area: web
created: 2026-06-02
refs:
  - web/package.json
  - web/package-lock.json
  - docs/rfcs/0048-operator-tester-web-console.md
  - https://github.com/advisories/GHSA-5xrq-8626-4rwp
  - https://github.com/advisories/GHSA-4w7w-66w2-5vf9
  - https://github.com/advisories/GHSA-67mh-4wv8-2f99
---

## Summary

The [RFC 0048](../rfcs/0048-operator-tester-web-console.md) web console added the repository's first JavaScript toolchain. GitHub Dependabot raised three open advisories against `web/package-lock.json`, all in dev/build tooling:

| Alert | Package | Range | Patched | Advisory | Nominal severity |
|-------|---------|-------|---------|----------|------------------|
| #13 | `vitest` | `< 4.1.0` | `4.1.0` | [GHSA-5xrq-8626-4rwp](https://github.com/advisories/GHSA-5xrq-8626-4rwp) — Vitest UI server allows arbitrary file read & execution | critical |
| #12 | `vite` | `<= 6.4.1` | `6.4.2` | [GHSA-4w7w-66w2-5vf9](https://github.com/advisories/GHSA-4w7w-66w2-5vf9) — path traversal in optimized-deps `.map` handling | medium |
| #11 | `esbuild` | `<= 0.24.2` | `0.25.0` | [GHSA-67mh-4wv8-2f99](https://github.com/advisories/GHSA-67mh-4wv8-2f99) — dev server accepts any cross-origin request and returns the response | medium |

## Context

The committed lockfile pins `vitest@2.1.9`, which transitively pulls `vite-node@2.1.9 → vite@5.4.21 → esbuild@0.21.5` — the vulnerable versions feeding alerts #11 and the vitest portion of the tree. (The top-level `vite` already resolves to a patched `6.4.x` locally, but `web/package-lock.json` on the default branch is the artifact Dependabot scans, so the alert tracks the lockfile state, not a developer's freshly-installed `node_modules`.)

The defining fact for triage is the **deployment shape**: the orchestrator ships a static, pre-built Svelte bundle embedded in the Go binary (the `internal/ui/assets` embed; `make ui` produces it, and the generated JS/CSS stay gitignored). `vitest`, `vite`, and `esbuild` are `devDependencies` — they exist only on a developer's or CI's machine. None of them is present in, or reachable from, the running orchestrator.

## Impact

- **No runtime / no shipped exposure.** The vulnerable packages never run in production. The attack surface is a developer or CI host running `vite` (dev server), `vite preview`, or the Vitest UI — not the deployed console.
- **The `critical` label overstates the real exposure here.** GHSA-5xrq-8626-4rwp requires the **Vitest UI server** to be listening. This project runs `vitest run` (headless) in both the npm script and CI, and has **no `@vitest/ui` dependency** (`npm ls @vitest/ui` → not present), so the exploit precondition is never met. Recorded as `medium` to reflect the actual project exposure (dev-tooling, no active exploit path), with the nominal advisory severity called out above so the discrepancy is explicit, not hidden.
- **esbuild / vite dev-server advisories are localhost-dev only.** They matter when a developer runs the dev server on an untrusted/shared network; `make ui` (the only path that touches the shipped artifact) is a one-shot build, not a long-lived server.

## Proposed fix / investigation path

1. **Bump the two mediums first — low risk.** `vite` to `>= 6.4.2` and let `esbuild` float to its patched `>= 0.25.0` via the lockfile. These are minor/patch bumps within the already-pinned major; re-run `make ui` + the web suite to confirm green.
2. **Bump `vitest` to `>= 4.1.0` — the only majorish change.** 2.x → 4.x is a major jump that may shift config (`vitest.config`/`vite.config`), the `@testing-library/svelte` peer range, and assertion/mocking behaviour. Do it on its own commit and re-validate the full 52-test suite (api / Chat / App / bootstrap). This bump also refreshes the transitive `vite-node → vite → esbuild` subtree that feeds alert #11.
3. **Regenerate the lockfile and confirm Dependabot clears.** `npm install` in `web/`, commit `web/package-lock.json`, push, and verify the three alerts close on the default branch.
4. **Consider enabling Dependabot version-update PRs** for the `web/` npm ecosystem so future toolchain CVEs arrive as PRs rather than ad-hoc findings — there is no `.github/dependabot.yml` ecosystem entry for npm yet.

> Pre-existing on `main` (surfaced on the push for [PR #501](https://github.com/mkhomutov/Persatrix/pull/501) but not introduced by its diff). Filed so the toolchain bumps are tracked rather than left to drift; the deployment shape makes this a hygiene item, not an incident.
