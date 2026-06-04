<script>
  // A single channel message row, extracted from ChannelTimeline.svelte so the
  // panel stays under the review-size cap. "from-self" right-aligns the
  // operator's own posts; senderLabel resolves persona ids to display names. The
  // content is split into segments so resolved `@id` mentions (RFC 0011) render
  // highlighted while the surrounding prose stays plain — no {@html}, so message
  // text can never inject markup.
  import { formatTimestamp, senderLabel } from "../lib/format.js";
  import { segmentMentions } from "../lib/mentions.js";

  let { message, userId, agentsById } = $props();

  const segments = $derived(
    segmentMentions(message.content, message.mentions ?? []),
  );
</script>

<li class="message" class:from-self={message.sender_id === userId}>
  <span class="sender">{senderLabel(message.sender_id, userId, agentsById)}</span>
  <span class="content"
    >{#each segments as segment}{#if segment.mention}<span class="mention"
          >{segment.text}</span
        >{:else}{segment.text}{/if}{/each}</span
  >
  <time class="ts" datetime={message.timestamp}
    >{formatTimestamp(message.timestamp)}</time
  >
</li>

<style>
  .mention {
    color: var(--accent, #2563eb);
    font-weight: 600;
  }
</style>
