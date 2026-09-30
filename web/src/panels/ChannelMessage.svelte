<script>
  // A single channel message row, extracted from ChannelTimeline.svelte so the
  // panel stays under the review-size cap. Rendered Slack-style: an initials
  // avatar (hue hashed from the sender id, stable across reloads), a sender +
  // time head, and the content beneath. "from-self" accents the operator's own
  // posts; senderLabel resolves persona ids to display names. The content is
  // split into segments so resolved `@id` mentions (RFC 0011) render
  // highlighted while the surrounding prose stays plain — no {@html}, so message
  // text can never inject markup.
  //
  // `compact` (set by ConversationFeed for a consecutive same-sender run) keeps
  // the visual thread tight: the avatar and head are hidden from view but stay
  // in the accessible tree (sr-only) so attribution survives for AT users; the
  // time shows in the gutter on hover instead. `dayLabel` (the first message of
  // a calendar day) draws the day divider above the row — inside the list item,
  // so the timeline's items stay one per message.
  import {
    formatTime,
    formatTimestamp,
    senderLabel,
    hueForId,
    initialsFor,
  } from "../lib/format.js";
  import { segmentMentions } from "../lib/mentions.js";

  let { message, userId, agentsById, compact = false, dayLabel = "" } = $props();

  const segments = $derived(
    segmentMentions(message.content, message.mentions ?? []),
  );

  const label = $derived(senderLabel(message.sender_id, userId, agentsById));
  // "Ada — Researcher" shows the name strong and the role quiet; the text of
  // the sender element stays the whole label.
  const namePart = $derived(label.split(" — ")[0]);
  const rolePart = $derived(label.includes(" — ") ? label.slice(namePart.length) : "");
  const initials = $derived(initialsFor(label));
  const hue = $derived(hueForId(message.sender_id));
  const time = $derived(formatTime(message.timestamp));
  const fullTime = $derived(formatTimestamp(message.timestamp));
</script>

<li
  class="message"
  class:from-self={message.sender_id === userId}
  class:compact
>
  {#if dayLabel}
    <div class="day-divider" role="separator" aria-label={dayLabel}>
      <span aria-hidden="true">{dayLabel}</span>
    </div>
  {/if}
  <div class="msg-row">
    <span class="avatar" style="--h: {hue}" aria-hidden="true">{initials}</span>
    {#if compact}
      <span class="gutter-time" aria-hidden="true" title={fullTime}>{time}</span>
    {/if}
    <div class="msg-body">
      <p class="msg-head" class:sr-only={compact}>
        <span class="sender">{namePart}{#if rolePart}<span class="sender-role">{rolePart}</span>{/if}</span>
        <time class="ts" datetime={message.timestamp} title={fullTime}>{time}</time>
      </p>
      <p class="content"
        >{#each segments as segment}{#if segment.mention}<span class="mention"
              >{segment.text}</span
            >{:else}{segment.text}{/if}{/each}</p
      >
    </div>
  </div>
</li>

<style>
  .message {
    list-style: none;
    padding-top: 0.6rem;
  }

  .message.compact {
    padding-top: 0.05rem;
  }

  .msg-row {
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.3rem 0.75rem;
    border-radius: var(--radius-sm, 8px);
    transition: background 100ms ease;
  }

  .msg-row:hover {
    background: var(--surface-muted, #f1f3f6);
  }

  .compact .msg-row {
    padding-top: 0.1rem;
    padding-bottom: 0.1rem;
  }

  .compact .avatar {
    visibility: hidden;
    height: 0;
  }

  .gutter-time {
    position: absolute;
    left: 0.75rem;
    width: 34px;
    top: 0.3rem;
    text-align: center;
    font-size: 0.66rem;
    color: var(--text-subtle, #8b92a2);
    opacity: 0;
    transition: opacity 100ms ease;
  }

  .msg-row:hover .gutter-time {
    opacity: 1;
  }

  .avatar {
    flex: none;
    width: 34px;
    height: 34px;
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.74rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: light-dark(hsl(var(--h) 48% 30%), hsl(var(--h) 60% 86%));
    background: light-dark(hsl(var(--h) 70% 90%), hsl(var(--h) 32% 27%));
    user-select: none;
  }

  .from-self .avatar {
    color: var(--accent-contrast, #fff);
    background: var(--accent, #5856d6);
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
    line-height: 1.35;
  }

  .sender {
    font-weight: 650;
    font-size: 0.875rem;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sender-role {
    font-weight: 450;
    color: var(--text-muted, #5d6576);
  }

  .from-self .sender {
    color: var(--accent-strong, #4745c2);
  }

  .ts {
    flex: none;
    font-size: 0.72rem;
    color: var(--text-subtle, #8b92a2);
  }

  .content {
    margin: 0.1rem 0 0;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
    line-height: 1.55;
  }

  .mention {
    color: var(--accent-strong, #4745c2);
    background: var(--accent-soft, #eeeefd);
    border-radius: 5px;
    padding: 0 0.25rem;
    font-weight: 600;
    /* Keep the pill on one line — the content's overflow-wrap:anywhere must
       not split a mention token across lines. */
    white-space: nowrap;
  }

  .day-divider {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin: 0.9rem 0 0.35rem;
    font-size: 0.72rem;
    font-weight: 650;
    color: var(--text-muted, #5d6576);
  }

  .day-divider::before,
  .day-divider::after {
    content: "";
    flex: 1;
    height: 1px;
    background: var(--border, #e3e6ec);
  }

  .day-divider span {
    padding: 0.1rem 0.65rem;
    border: 1px solid var(--border, #e3e6ec);
    border-radius: 999px;
    background: var(--surface, #fff);
  }
</style>
