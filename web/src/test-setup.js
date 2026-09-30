// Vitest setup for every test file (vite.config.js `test.setupFiles`). The
// console keeps per-browser preferences in localStorage (lib/prefs.js), and a
// test file's tests share one jsdom — so start each test from a clean browser,
// or one test's remembered channel or folded rail leaks into the next.
import { beforeEach } from "vitest";

beforeEach(() => {
  localStorage.clear();
});
