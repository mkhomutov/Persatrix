<script>
  // The navigation rail — the two ways into a conversation (DM a persona /
  // watch a group channel) plus the acting-identity echo. Extracted from
  // ChannelTimeline.svelte to keep the panel under the review-size cap, the
  // same reason PersonaPicker / ChannelPicker themselves were split out.
  //
  // selectedAgent / selectedChannel — bound through to the pickers.
  // agents / groupChannels — the picker option sources.
  // sending — locks the persona picker while a DM turn is in flight.
  // canCreate — gates the "New channel" affordance.
  // userId — the effective identity echoed at the rail foot (§E/§F).
  // onPersonaPick / onExit / onChannelChange / onRefreshAgents /
  // onRefreshChannels / onNewChannel — the panel's handlers, threaded through.
  import PersonaPicker from "./PersonaPicker.svelte";
  import NoPersonasHint from "./NoPersonasHint.svelte";
  import ChannelPicker from "./ChannelPicker.svelte";

  let {
    selectedAgent = $bindable(),
    selectedChannel = $bindable(),
    agents = [],
    groupChannels = [],
    sending = false,
    canCreate = false,
    userId,
    onPersonaPick,
    onExit,
    onChannelChange,
    onRefreshAgents,
    onRefreshChannels,
    onNewChannel,
  } = $props();
</script>

<aside class="rail" aria-label="Conversations">
  <div class="rail-section">
    <h3 class="rail-title">Direct message</h3>
    {#if agents.length > 0}
      <!-- DM entry point (§B): pick a persona to start/open a direct message. -->
      <PersonaPicker
        bind:selectedAgent
        {agents}
        {sending}
        onChange={onPersonaPick}
        {onExit}
      />
    {:else}
      <!-- No personas + channels exist: the DM entry point (why + cloud-demo
           cause) is in NoPersonasHint. -->
      <NoPersonasHint onRefresh={onRefreshAgents} />
    {/if}
  </div>

  <div class="rail-section">
    <h3 class="rail-title">Channels</h3>
    <ChannelPicker
      {groupChannels}
      bind:selectedChannel
      {canCreate}
      {onChannelChange}
      onRefresh={onRefreshChannels}
      {onNewChannel}
    />
  </div>

  <p class="identity rail-foot">Acting as <code>{userId}</code></p>
</aside>
