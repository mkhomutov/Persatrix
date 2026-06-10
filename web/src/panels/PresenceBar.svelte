<script>
  // The live status line above the composer (RFC 0048 console). Purely
  // presentational: the owner (ConversationFeed) tracks who is working and when
  // the turn flips back to the operator, and hands this the resolved state. Two
  // mutually-exclusive states, in the same slot for both DMs and group channels:
  //
  //   • thinking — one or more personas have an in-flight turn. The animated dot
  //     signals liveness between the 3s polls; the copy softens to "taking a
  //     while…" (slow) so a slow reply doesn't read as a stall. A DM turn can be
  //     cancelled (onCancel), abandoning the synchronous round-trip.
  //   • idle — a brief "Waiting for you" hint the owner flashes when a turn just
  //     completed, then clears, so the ball-in-your-court moment is visible
  //     without a permanent line of chrome.
  //
  // Renders nothing when neither holds. The phrasing comes from lib/presence.js
  // so it is unit-tested independently of this render.
  import { thinkingPhrase } from "../lib/presence.js";

  // thinking — agent ids with an in-flight turn (empty = none); agentsById —
  // name resolution; slow — soften the copy past the slow threshold; idle —
  // flash the "Waiting for you" hint; onCancel — when set (a DM send in flight),
  // render a Cancel control wired to it.
  let {
    thinking = [],
    agentsById = {},
    slow = false,
    idle = false,
    onCancel = null,
  } = $props();

  const active = $derived((thinking ?? []).filter(Boolean));
  const phrase = $derived(thinkingPhrase(active, agentsById, { slow }));
</script>

{#if active.length > 0}
  <p class="presence thinking" role="status" aria-live="polite">
    <span class="pulse" aria-hidden="true"></span>
    {phrase}
    {#if onCancel}
      <button type="button" class="cancel" onclick={onCancel}>Cancel</button>
    {/if}
  </p>
{:else if idle}
  <p class="presence idle" role="status" aria-live="polite">Waiting for you</p>
{/if}

<style>
  .presence {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.25rem 0;
    opacity: 0.7;
    font-style: italic;
    font-size: 0.9rem;
  }

  /* A small breathing dot carries the "live" signal — gentler than animating
     the ellipsis text (which would re-fire the aria-live region). */
  .pulse {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--accent, #2563eb);
    flex: none;
    animation: presence-pulse 1.4s ease-in-out infinite;
  }

  .cancel {
    margin-left: 0.25rem;
    font: inherit;
    font-style: normal;
  }

  @keyframes presence-pulse {
    0%,
    100% {
      opacity: 0.25;
    }
    50% {
      opacity: 1;
    }
  }

  /* Respect a reduced-motion preference: hold the dot steady rather than pulse. */
  @media (prefers-reduced-motion: reduce) {
    .pulse {
      animation: none;
      opacity: 0.7;
    }
  }
</style>
