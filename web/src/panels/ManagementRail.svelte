<script>
  // The management rail — roster + governance for the watched group channel,
  // rehomed from inline disclosures above the feed. Extracted from
  // ChannelTimeline.svelte to keep the panel under the review-size cap. The
  // capability gates are unchanged: `create` renders the Members card,
  // `config_edit` the Channel-settings card (each already reduced to
  // enabled && available by the shell); the parent renders this rail only for
  // a watched non-DM channel.
  import ChannelMembers from "./ChannelMembers.svelte";
  import ChannelSettings from "./ChannelSettings.svelte";

  let {
    channelId,
    members = [],
    agents = [],
    agentsById = {},
    userId,
    canCreate = false,
    canConfigEdit = false,
    onChanged,
  } = $props();
</script>

<aside class="details-rail" aria-label="Channel management">
  {#if canCreate}
    <ChannelMembers {channelId} {members} {agents} {agentsById} {userId} {onChanged} />
  {/if}
  {#if canConfigEdit}
    <ChannelSettings {channelId} {members} {agentsById} {onChanged} />
  {/if}
</aside>
