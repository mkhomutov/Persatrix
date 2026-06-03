// Sticky cross-mount panel selection (RFC 0048).
//
// The console mounts only the active panel (App.svelte: "Only the active panel
// is mounted"), so a panel's local $state is destroyed every time the operator
// switches tabs and re-created — re-running its default-selection logic — when
// they switch back. That silently resets a deliberate choice on each Chat↔
// Channels round-trip. This module holds those choices in module-level $state,
// which outlives an unmount, so a panel can resume where the operator left it.
//
// A `.svelte.js` module so the `$state` rune is reactive across components,
// mirroring nav.svelte.js. "" means no remembered choice — the panel then
// applies its own default. Persistence is in-memory (per page load), matching
// the bug it fixes (a tab switch, not a reload).
export const selection = $state({
  // The chat panel's last deliberately-selected persona id. Set on a user-driven
  // persona change (not the programmatic default), so a never-chosen panel still
  // gets the smart healthy-first default rather than a frozen first selection.
  chatAgent: "",
});
