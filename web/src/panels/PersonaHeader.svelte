<script>
  // Persona header (RFC 0048 amendment §A): gives the conversation a face. Name +
  // role identify the persona and the capability chips say what it's for — all
  // from fields the agent DTO already carries (§A). Rendered above a DM in the
  // consolidated Channels panel — the sole conversation surface now.
  //
  // The §F "view in timeline" deep-link this header used to carry is gone
  // (RFC 0048 chat-panel-retirement amendment §C): a DM IS a channel selection in
  // the one conversation panel now, so there is no second panel to hand off to.
  //
  // info — the persona record behind the picker selection; nothing renders until
  //        it resolves.
  import { hueForId, initialsFor } from "../lib/format.js";

  let { info } = $props();

  const displayName = $derived(info ? info.name || info.id : "");
</script>

{#if info}
  <header class="persona">
    <span
      class="persona-avatar"
      style="--h: {hueForId(info.id)}"
      aria-hidden="true">{initialsFor(displayName)}</span
    >
    <span class="persona-name">{displayName}</span>
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
  </header>
{/if}
