<script>
  // A single channel message row, extracted from ChannelTimeline.svelte so the
  // panel stays under the review-size cap. Rendered Slack-style: an initials
  // avatar (hue hashed from the sender id, stable across reloads), a sender +
  // timestamp head, and the content beneath. "from-self" accents the operator's
  // own posts; senderLabel resolves persona ids to display names. The content
  // is split into segments so resolved `@id` mentions (RFC 0011) render
  // highlighted while the surrounding prose stays plain — no {@html}, so message
  // text can never inject markup.
  //
  // `compact` (set by ConversationFeed for a consecutive same-sender run) keeps
  // the visual thread tight: the avatar and head are hidden from view but stay
  // in the accessible tree (sr-only) so attribution survives for AT users.
  import {
    formatTimestamp,
    senderLabel,
    hueForId,
    initialsFor,
  } from "../lib/format.js";
  import { segmentMentions } from "../lib/mentions.js";

  let { message, userId, agentsById, compact = false } = $props();

  const segments = $derived(
    segmentMentions(message.content, message.mentions ?? []),
  );

  const label = $derived(senderLabel(message.sender_id, userId, agentsById));
  const initials = $derived(initialsFor(label));
  const hue = $derived(hueForId(message.sender_id));
</script>

<li
  class="message"
  class:from-self={message.sender_id === userId}
  class:compact
>
  <span class="avatar" style="--h: {hue}" aria-hidden="true">{initials}</span>
  <div class="msg-body">
    <p class="msg-head" class:sr-only={compact}>
      <span class="sender">{label}</span>
      <time class="ts" datetime={message.timestamp}
        >{formatTimestamp(message.timestamp)}</time
      >
    </p>
    <p class="content"
      >{#each segments as segment}{#if segment.mention}<span class="mention"
            >{segment.text}</span
          >{:else}{segment.text}{/if}{/each}</p
    >
  </div>
</li>

<style>
  .message {
    display: flex;
    align-items: flex-start;
    gap: 0.65rem;
    padding: 0.35rem 0.5rem;
    border-radius: var(--radius-sm, 7px);
    margin-top: 0.55rem;
  }

  .message.compact {
    margin-top: 0;
    padding-top: 0.1rem;
    padding-bottom: 0.1rem;
    /* Keep the content aligned with the non-compact rows above it:
       avatar width + gap. */
    padding-left: calc(0.5rem + 30px + 0.65rem);
  }

  .message.compact .avatar {
    display: none;
  }

  .message.from-self {
    background: var(--accent-soft, #eef0fe);
  }

  .avatar {
    flex: none;
    width: 30px;
    height: 30px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: light-dark(hsl(var(--h) 45% 28%), hsl(var(--h) 50% 85%));
    background: light-dark(hsl(var(--h) 65% 88%), hsl(var(--h) 30% 26%));
    user-select: none;
  }

  .from-self .avatar {
    color: var(--accent-contrast, #fff);
    background: var(--accent, #4f46e5);
  }

  .msg-body {
    min-width: 0;
    flex: 1;
  }

  .msg-head {
    margin: 0;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
  }

  .sender {
    font-weight: 650;
    font-size: 0.88rem;
  }

  .from-self .sender {
    color: var(--accent-strong, #4338ca);
  }

  .ts {
    font-size: 0.72rem;
    color: var(--text-muted, #6b7280);
  }

  .content {
    margin: 0.05rem 0 0;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .mention {
    color: var(--accent-strong, #4338ca);
    background: var(--accent-soft, #eef0fe);
    border-radius: 5px;
    padding: 0 0.25rem;
    font-weight: 600;
    /* Keep the pill on one line — the content's overflow-wrap:anywhere must
       not split a mention token across lines. */
    white-space: nowrap;
  }

  .from-self .mention {
    background: light-dark(rgba(255, 255, 255, 0.6), rgba(0, 0, 0, 0.25));
  }
</style>
