// Cross-panel navigation intent (RFC 0048 amendment §F).
//
// The console's two hero panels are otherwise siloed — only the active panel is
// mounted, and the channel timeline picks its own channel. To let the Chat panel
// hand a specific conversation to the timeline ("view this conversation in the
// timeline"), Chat records the target DM channel id here and switches the hash
// route to #/channels; the freshly-mounted timeline reads and clears it on load.
//
// A `.svelte.js` module so the `$state` rune is reactive across components. The
// single field is the pending channel selection; "" means no pending intent.
export const nav = $state({ targetChannel: "" });
