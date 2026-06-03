<script>
  // A single channel message row, extracted from ChannelTimeline.svelte so the
  // panel stays under the review-size cap. "from-self" right-aligns the
  // operator's own posts; senderLabel resolves persona ids to display names.
  import { formatTimestamp, senderLabel } from "../lib/format.js";

  let { message, userId, agentsById } = $props();
</script>

<li class="message" class:from-self={message.sender_id === userId}>
  <span class="sender">{senderLabel(message.sender_id, userId, agentsById)}</span>
  <span class="content">{message.content}</span>
  <time class="ts" datetime={message.timestamp}
    >{formatTimestamp(message.timestamp)}</time
  >
</li>
