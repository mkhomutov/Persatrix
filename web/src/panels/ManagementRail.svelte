<script>
  // The management rail — roster + governance for the watched group channel,
  // rehomed from inline disclosures above the feed. Extracted from
  // ChannelTimeline.svelte to keep the panel under the review-size cap. The
  // capability gates are unchanged: `create` renders the Members card,
  // `config_edit` the Channel-settings card (each already reduced to
  // enabled && available by the shell); the parent renders this rail only for
  // a watched non-DM channel.
  //
  // hidden  — folded away from the header toggle. The rail stays mounted while
  //           folded, so unsaved settings survive a look at the conversation.
  // onClose — fold it from its own close button.
  import ChannelMembers from "./ChannelMembers.svelte";
  import ChannelSettings from "./ChannelSettings.svelte";
  import Icon from "../ui/Icon.svelte";

  let {
    channelId,
    members = [],
    agents = [],
    agentsById = {},
    userId,
    canCreate = false,
    canConfigEdit = false,
    hidden = false,
    onChanged,
    onClose,
  } = $props();
</script>

<aside class="details-rail" aria-label="Channel management" {hidden}>
  <div class="details-head">
    <h2 class="details-title">Details</h2>
    <button type="button" class="btn-icon btn-ghost" aria-label="Close details" onclick={onClose}>
      <Icon name="x" size={16} />
    </button>
  </div>
  <div class="details-body">
    {#if canCreate}
      <ChannelMembers {channelId} {members} {agents} {agentsById} {userId} {onChanged} />
    {/if}
    {#if canConfigEdit}
      <ChannelSettings {channelId} {members} {agentsById} {onChanged} />
    {/if}
  </div>
</aside>
