<script>
  // Group-channel selector + toolbar (Refresh / New channel), extracted from
  // ChannelTimeline.svelte so the panel stays under the review-size cap (the
  // same reason PublishComposer / PersonaPicker were split out). Markup is
  // unchanged from the inline version — the panel's create/selection tests
  // drive these controls through ChannelTimeline.
  //
  // groupChannels    — the group (non-DM) channels to choose from.
  // selectedChannel  — bound: the watched channel id.
  // canCreate        — gates the "New channel" affordance.
  // onChannelChange  — fired when the operator picks a different channel.
  // onRefresh        — re-list channels.
  // onNewChannel     — open the create form.
  import { channelLabel } from "../lib/format.js";

  let {
    groupChannels = [],
    selectedChannel = $bindable(""),
    canCreate = false,
    onChannelChange,
    onRefresh,
    onNewChannel,
  } = $props();
</script>

{#if groupChannels.length > 0}
  <div class="channel-picker">
    <label>
      <!-- The rail section already titles this "Channels"; the label text stays
           for the select's accessible name without a second visible line. -->
      <span class="sr-only">Channel</span>
      <select bind:value={selectedChannel} onchange={onChannelChange}>
        {#each groupChannels as channel (channel.id)}
          <option value={channel.id}>{channelLabel(channel)}</option>
        {/each}
      </select>
    </label>
    <div class="picker-actions">
      <button type="button" class="refresh" onclick={onRefresh}>Refresh</button>
      {#if canCreate}
        <button type="button" class="new-channel" onclick={onNewChannel}>
          New channel
        </button>
      {/if}
    </div>
  </div>
{:else if canCreate}
  <!-- No group channels yet, but the operator can make one. -->
  <div class="channel-picker">
    <div class="picker-actions">
      <button type="button" class="new-channel" onclick={onNewChannel}>
        New channel
      </button>
      <button type="button" class="refresh" onclick={onRefresh}>Refresh</button>
    </div>
  </div>
{/if}
