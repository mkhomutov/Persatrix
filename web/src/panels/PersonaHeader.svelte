<script>
  // Persona header (RFC 0048 amendment §A + §F): gives the conversation a face.
  // Name + role identify the persona and the capability chips say what it's for
  // — all from fields the agent DTO already carries (§A). Once a conversation
  // exists, a "View in timeline" affordance hands the persisted DM channel to
  // the timeline panel (§F). Extracted from Chat.svelte so the panel stays
  // under the review-size cap.
  //
  // info — the persona record behind the picker selection; nothing renders
  //        until it resolves.
  // dmChannelId — the resolved DM channel id; gates the §F hand-off affordance.
  // onViewInTimeline — handler invoked to hand this conversation to the timeline.
  let { info, dmChannelId, onViewInTimeline } = $props();
</script>

{#if info}
  <header class="persona">
    <span class="persona-name">{info.name || info.id}</span>
    {#if info.role}
      <span class="persona-role">{info.role}</span>
    {/if}
    {#if info.capabilities && info.capabilities.length > 0}
      <ul class="persona-caps" aria-label="Capabilities">
        <!-- Unkeyed: capabilities are display-only and the registry doesn't
             dedupe them, so a value key would throw each_key_duplicate. The
             list is re-derived wholesale per selection, so there's no identity
             to preserve across mutations anyway. -->
        {#each info.capabilities as capability}
          <li>{capability}</li>
        {/each}
      </ul>
    {/if}
    {#if dmChannelId}
      <!-- Cross-panel continuity (§F): this conversation is a persisted DM
           channel — jump to the timeline to watch it as one. A real <a> (it
           navigates to another view), so assistive tech announces it as a link
           and the destination shows on hover; href is the timeline route.
           onViewInTimeline records which DM the freshly-mounted timeline should
           open and drives the route change (it preventDefaults the native nav
           so the intent is always recorded first). Only shown once a
           conversation exists (dmChannelId resolved from history). -->
      <a href="#/channels" class="link-like" onclick={onViewInTimeline}>
        View in timeline ↗
      </a>
    {/if}
  </header>
{/if}
