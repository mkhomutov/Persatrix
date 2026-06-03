<script>
  // A single chat transcript row, extracted from Chat.svelte so the panel stays
  // under the review-size cap. Renders the message bubble (user / agent / error
  // variants, plus the "empty reply" placeholder) and a meta line with the
  // timestamp and any per-turn isolation scope (RFC 0031 session / ISSUE-0085
  // epoch).
  import { formatTimestamp } from "../lib/format.js";

  let { msg } = $props();
</script>

<li class="msg">
  <p
    class:from-user={msg.fromUser}
    class:from-agent={!msg.fromUser}
    class:reply-error={msg.status === "error"}
  >
    <strong>{msg.who}:</strong>
    {#if !msg.fromUser && msg.status === "empty"}
      <!-- reply_status:"empty" is a valid message (the agent had nothing
           to say, chat_handler.go) — show a placeholder so it doesn't
           read as a blank/broken line. -->
      <em class="empty-reply">(no reply)</em>
    {:else}
      {msg.content}
    {/if}
  </p>
  <p class="msg-meta">
    {#if msg.timestamp}
      <time datetime={msg.timestamp}>{formatTimestamp(msg.timestamp)}</time>
    {/if}
    <!-- Per-message isolation scope (RFC 0031 session / ISSUE-0085
         epoch). Only live turns carry it; seeded history shows no scope
         line (amendment §B caveat 2). -->
    {#if msg.session}<span>session: {msg.session}</span>{/if}
    {#if msg.epoch}<span>epoch: {msg.epoch}</span>{/if}
  </p>
</li>
