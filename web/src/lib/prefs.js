// Per-browser console preferences — the colour theme, layout choices such as
// whether the details rail is open, and the last watched channel. They are
// conveniences, never state the console depends on: storage can be missing,
// full or blocked (private windows, cleared site data), so every read falls
// back to its default and every write fails silently.

const PREFIX = "persatrix.console.";

function defaultStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function readPref(key, fallback, storage = defaultStorage()) {
  try {
    const raw = storage?.getItem(PREFIX + key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function writePref(key, value, storage = defaultStorage()) {
  try {
    storage?.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    // A convenience only — the console works the same without it.
  }
}

// The colour theme: "system" follows the operating system; "light" and
// "dark" pin it.
export const THEMES = ["system", "light", "dark"];

export function nextTheme(theme) {
  const i = THEMES.indexOf(theme);
  return i === -1 ? "system" : THEMES[(i + 1) % THEMES.length];
}

export function loadTheme(storage = defaultStorage()) {
  const theme = readPref("theme", "system", storage);
  return THEMES.includes(theme) ? theme : "system";
}

// applyTheme pins light or dark through `data-theme` on the root element (the
// token sheet keys `color-scheme` off it); "system" removes the pin.
export function applyTheme(theme, root = document.documentElement) {
  if (theme === "light" || theme === "dark") {
    root.dataset.theme = theme;
  } else {
    delete root.dataset.theme;
  }
}
