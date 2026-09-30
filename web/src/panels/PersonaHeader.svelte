<script>
  // Persona header (RFC 0048 amendment §A): gives the conversation a face. Name +
  // role identify the persona and the capability chips say what it's for — all
  // from fields the agent DTO already carries (§A). Rendered in the header of a
  // DM in the consolidated Channels panel — the sole conversation surface now.
  // The name is the conversation's heading, as a group channel's #name is.
  //
  // The §F "view in timeline" deep-link this header used to carry is gone
  // (RFC 0048 chat-panel-retirement amendment §C): a DM IS a channel selection in
  // the one conversation panel now, so there is no second panel to hand off to.
  //
  // info — the persona record behind the selection; nothing renders until it
  //        resolves.
  import Avatar from "../ui/Avatar.svelte";

  let { info } = $props();

  const displayName = $derived(info ? info.name || info.id : "");
  // Only a non-healthy status is worth words; healthy shows as the green dot.
  const statusNote = $derived(
    info?.status && info.status !== "healthy" ? info.status : "",
  );
</script>

{#if info}
  <div class="persona">
    <Avatar id={info.id} label={displayName} size={34} status={info.status} />
    <div class="persona-text">
      <h2 class="persona-name">{displayName}</h2>
      {#if info.role || statusNote}
        <p class="persona-role">
          {info.role}{#if info.role && statusNote}{" · "}{/if}{#if statusNote}<span class="persona-status">{statusNote}</span>{/if}
        </p>
      {/if}
    </div>
    {#if info.capabilities && info.capabilities.length > 0}
      <ul class="persona-caps" aria-label="Capabilities">
        <!-- Unkeyed: capabilities are display-only and the registry doesn't
             dedupe them, so a value key would throw each_key_duplicate. The
             list is re-derived wholesale per selection, so there's no identity
             to preserve across mutations anyway. -->
        {#each info.capabilities as capability}
          <li class="chip">{capability}</li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}
