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
  // Three states: an id (resume it), "" (never chose — apply the default), and
  // null (the operator hit Exit — stay in the lobby across a tab switch). See
  // pickInitialAgent.
  chatAgent: "",
});

// pickInitialAgent resolves which persona a freshly-mounted Chat panel opens on.
// It prefers the operator's last deliberately-chosen persona (rememberedId, from
// selection.chatAgent) when that persona is still in the list, so a Chat↔Channels
// round-trip resumes where they left off rather than snapping to the default.
// Otherwise it falls to the first persona that is BOTH chattable and healthy — a
// newcomer lands on a sendable conversation, never a disabled task agent or a
// guaranteed-503 offline one — then any chattable, then any agent at all (the
// composer then explains why it's locked). A remembered persona that has since
// deregistered isn't in the list, so it degrades to this default too. The caller
// passes its own isChattable predicate so the policy stays UI-agnostic.
//
// rememberedId === null is the EXITED sentinel: the operator deliberately left
// the conversation (the Exit affordance), so the panel must open on no persona
// (the lobby) and not snap to a default — that exit has to survive the unmount a
// tab switch causes. "" is the distinct never-chosen state, which still defaults.
// Returns null for an empty list or an explicit exit.
export function pickInitialAgent(list, isChattable, rememberedId) {
  if (list.length === 0) return null;
  if (rememberedId === null) return null;
  const chattable = list.filter(isChattable);
  const remembered = rememberedId
    ? list.find((agent) => agent.id === rememberedId)
    : null;
  return (
    remembered ??
    chattable.find((agent) => agent.status === "healthy") ??
    chattable[0] ??
    list[0]
  );
}
