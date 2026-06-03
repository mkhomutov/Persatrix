// Sticky cross-mount panel selection (RFC 0048).
//
// The console mounts only the active panel (App.svelte: "Only the active panel
// is mounted"), so a panel's local $state is destroyed every time the operator
// switches tabs and re-created — re-running its default-selection logic — when
// they switch back. That silently resets a deliberate choice on each tab
// round-trip. This module holds those choices in module-level $state, which
// outlives an unmount, so a panel can resume where the operator left it.
//
// A `.svelte.js` module so the `$state` rune is reactive across components.
// Persistence is in-memory (per page load), matching the bug it fixes (a tab
// switch, not a reload).
export const selection = $state({
  // The consolidated Channels panel's last deliberately-opened DM persona id
  // (RFC 0048 chat-panel-retirement amendment §B — the rehomed sticky selection).
  // Three states: an id resumes that DM across the tab unmount, "" means "never
  // opened a DM — show the group-channel view by default", and null is the
  // explicit "exited the DM" sentinel that must survive a tab switch (so a
  // remount lands on the group view, not back in a DM). Held here rather than in
  // the panel's local $state, which is destroyed on every tab round-trip.
  dmAgent: "",
});
