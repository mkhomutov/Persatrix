import { describe, it, expect } from "vitest";
import {
  readPref,
  writePref,
  THEMES,
  nextTheme,
  loadTheme,
  applyTheme,
} from "./prefs.js";

// Per-browser console preferences (colour theme, whether the details rail is
// open). They are conveniences, so storage that is missing, full or blocked
// (private windows, disabled site data) must never break the console: every
// read falls back to its default and every write fails silently.
function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    data,
  };
}

const throwing = {
  getItem() {
    throw new Error("blocked");
  },
  setItem() {
    throw new Error("blocked");
  },
};

describe("readPref / writePref", () => {
  it("round-trips a JSON value under a console-scoped key", () => {
    const storage = memoryStorage();
    writePref("detailsOpen", false, storage);
    expect(readPref("detailsOpen", true, storage)).toBe(false);
    expect(Object.keys(storage.data)[0]).toMatch(/^persatrix\.console\./);
  });

  it("falls back to the default for a missing or corrupt value", () => {
    const storage = memoryStorage({ "persatrix.console.x": "{not json" });
    expect(readPref("x", 7, storage)).toBe(7);
    expect(readPref("missing", "d", storage)).toBe("d");
  });

  it("never throws when storage is unavailable", () => {
    expect(readPref("x", "fallback", throwing)).toBe("fallback");
    expect(() => writePref("x", 1, throwing)).not.toThrow();
    expect(readPref("x", "fallback", null)).toBe("fallback");
  });
});

describe("theme", () => {
  it("cycles system → light → dark → system", () => {
    expect(THEMES).toEqual(["system", "light", "dark"]);
    expect(nextTheme("system")).toBe("light");
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("system");
    expect(nextTheme("bogus")).toBe("system");
  });

  it("loads a stored theme and ignores an unknown one", () => {
    expect(loadTheme(memoryStorage({ "persatrix.console.theme": '"dark"' }))).toBe(
      "dark",
    );
    expect(loadTheme(memoryStorage({ "persatrix.console.theme": '"neon"' }))).toBe(
      "system",
    );
    expect(loadTheme(throwing)).toBe("system");
  });

  it("pins light/dark on the root element and clears it for system", () => {
    const root = document.createElement("html");
    applyTheme("dark", root);
    expect(root.dataset.theme).toBe("dark");
    applyTheme("system", root);
    expect(root.dataset.theme).toBeUndefined();
  });
});
