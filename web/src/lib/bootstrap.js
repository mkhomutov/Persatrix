// Boot logic for the embedded web console (RFC 0048 Phase 1 / Slice 1).
//
// The SPA boots off two read-only endpoints: GET /api/v1/ui/config (which
// panels to render) and GET /api/v1/ui/context (who the principal is). The
// decisions made from those payloads — which panels to draw and what user_id
// to act as — are the load-bearing forward-compat rules from RFC §C/§F, kept
// here as pure functions so they are unit-testable without a browser.

// KNOWN_PANELS is the client's allow-list, in display order. The server's
// /ui/config payload is authoritative about enabled/available, but the client
// renders only panels it actually knows how to draw — so an older binary
// serving a newer bundle (or vice versa) degrades gracefully instead of
// throwing on an unrecognised panel name (RFC §C unknown-panel rule). The names
// match config/ui.yaml + schemas/ui.schema.json; Slice 1 ships only
// channel_timeline as a real panel — the single consolidated conversation
// surface hosting group channels + DMs (RFC 0048 chat-panel-retirement
// amendment; the standalone Chat panel was retired). memory_strip (Slice 2) and
// cost (Slice 4) are placeholders that ship dark.
export const KNOWN_PANELS = [
  { name: "channel_timeline", title: "Channels", route: "#/channels" },
  { name: "memory_strip", title: "Memory", route: "#/memory" },
  { name: "cost", title: "Cost", route: "#/cost" },
];

// selectPanels returns the descriptors for panels the console should render,
// in KNOWN_PANELS order. A panel renders only when the server reports it both
// `enabled` (operator toggle, config/ui.yaml) AND `available` (runtime-derived
// — the backing subsystem is wired). Unknown panel names in the payload are
// ignored. A missing/empty/nullish config yields an empty list rather than
// throwing, so a degraded /ui/config fetch shows an empty shell, not a crash.
export function selectPanels(config) {
  const panels = config?.panels;
  if (!panels) {
    return [];
  }
  return KNOWN_PANELS.filter((panel) => {
    const status = panels[panel.name];
    return Boolean(status?.enabled && status?.available);
  }).map((panel) => {
    // Thread the server-reported per-panel capabilities onto the descriptor so
    // the shell can pass them to the panel, which renders each affordance only
    // when both <cap>.enabled && <cap>.available. Two capabilities ride here:
    //   - `create` (RFC 0048 channel-creation amendment §A)
    //   - `config_edit` (RFC 0050 Phase 2 — the channel settings panel)
    // Each is spread under its own snake_case key, matching the server JSON and
    // App.svelte's `activePanel?.<cap>` access. A panel the server reports no
    // object for leaves that key undefined — the descriptor never fabricates a
    // capability the server didn't report.
    const entry = panels[panel.name];
    let descriptor = panel;
    if (entry?.create) {
      descriptor = { ...descriptor, create: entry.create };
    }
    if (entry?.config_edit) {
      descriptor = { ...descriptor, config_edit: entry.config_edit };
    }
    return descriptor;
  });
}

// deriveUserId resolves the user_id the console acts as from the /ui/context
// principal — the single identity source (RFC §F rule 1). The console never
// hard-codes or prompts for a user, so a missing/empty principal returns null
// (an error state the shell surfaces) rather than a fabricated default. Today
// the principal is the degenerate single-tenant "local"; an RFC 0039 auth
// layer later populates it with the real authenticated principal, unchanged
// here.
export function deriveUserId(context) {
  const principal = context?.principal;
  return principal ? principal : null;
}
