<script>
  // DM composer (RFC 0048 chat-panel-retirement amendment §B) — the chat-façade
  // send path rehomed onto the consolidated Channels panel. A message textarea,
  // the optional isolation ScopeSelector (RFC 0031 session / ISSUE-0085 epoch),
  // and a Send that locks for the synchronous turn. Mirrors PublishComposer's
  // prop shape (so the panel can swap composers by mode): the panel owns the
  // sendChat call and the abortable "Waiting…" status, so this is the form only.
  import ScopeSelector from "./ScopeSelector.svelte";

  // message/sessionId/epochId are bindable so the panel reads back the draft and
  // the scope each turn is sent under. sending locks the controls while a turn is
  // in flight (the synchronous chat call can block up to the server timeout, so
  // leaving the box editable would let the post-send reset wipe in-flight text).
  // chattable/hasPersona gate the Send + the task-agent notice; onSubmit/onKeydown
  // are the form-submit + Enter-to-send handlers.
  let {
    message = $bindable(),
    sessionId = $bindable(""),
    epochId = $bindable(""),
    sending,
    canSend,
    chattable,
    hasPersona,
    onSubmit,
    onKeydown,
  } = $props();
</script>

<form class="composer" onsubmit={onSubmit}>
  <label>
    Message
    <textarea
      bind:value={message}
      rows="3"
      placeholder="Say something to the persona… (Enter to send, Shift+Enter for a new line)"
      disabled={sending}
      onkeydown={onKeydown}
    ></textarea>
  </label>

  <!-- Optional isolation overrides (RFC 0031 session / ISSUE-0085 epoch). The §F
       identity rule constrains user_id only (never typed); session and epoch are
       operator-namespace ids the panel reads back via the bindings. -->
  <ScopeSelector bind:sessionId bind:epochId {sending} />

  {#if hasPersona && !chattable}
    <!-- Only reachable when the deployment has no persona to fall back to:
         explain why the composer is locked rather than leaving a dead Send. -->
    <p class="poll-error" role="status">
      Task agents run workflow steps and don't hold conversations — pick a
      persona to chat.
    </p>
  {/if}

  <button type="submit" disabled={!canSend || !chattable}>
    {sending ? "Sending…" : "Send"}
  </button>
</form>
