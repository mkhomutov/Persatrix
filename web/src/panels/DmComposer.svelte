<script>
  // DM composer (RFC 0048 chat-panel-retirement amendment §B) — the chat-façade
  // send path rehomed onto the consolidated Channels panel. A message textarea,
  // the optional isolation ScopeSelector (RFC 0031 session / ISSUE-0085 epoch),
  // and a Send that locks for the synchronous turn. Mirrors PublishComposer's
  // prop shape (so the panel can swap composers by mode): the panel owns the
  // sendChat call and the abortable "Waiting…" status, so this is the form only.
  import ScopeSelector from "./ScopeSelector.svelte";
  import Icon from "../ui/Icon.svelte";
  import { MAX_MESSAGE_LENGTH } from "../lib/dmSession.svelte.js";

  // message/sessionId/epochId are bindable so the panel reads back the draft and
  // the scope each turn is sent under. sending locks the controls while a turn is
  // in flight (the synchronous chat call can block up to the server timeout, so
  // leaving the box editable would let the post-send reset wipe in-flight text).
  // chattable/hasPersona gate the Send + the task-agent notice; personaName
  // addresses the placeholder; onSubmit/onKeydown are the form-submit +
  // Enter-to-send handlers.
  let {
    message = $bindable(),
    sessionId = $bindable(""),
    epochId = $bindable(""),
    sending,
    canSend,
    chattable,
    hasPersona,
    personaName = "",
    onSubmit,
    onKeydown,
  } = $props();

  // The server counts code points, so count the same way; show the counter
  // only in the last stretch before the limit, where it helps.
  const COUNTER_FROM = 3500;
  const length = $derived([...(message ?? "")].length);
</script>

<form class="composer" onsubmit={onSubmit}>
  <label>
    <span class="sr-only">Message</span>
    <textarea
      bind:value={message}
      rows="1"
      placeholder={personaName ? `Message ${personaName}…` : "Say something to the persona…"}
      disabled={sending}
      onkeydown={onKeydown}
    ></textarea>
  </label>

  {#if hasPersona && !chattable}
    <!-- Only reachable when the deployment has no persona to fall back to:
         explain why the composer is locked rather than leaving a dead Send. -->
    <p class="poll-error composer-note" role="status">
      Task agents run workflow steps and don't hold conversations — pick a
      persona to chat.
    </p>
  {/if}

  <div class="composer-actions">
    <!-- Optional isolation overrides (RFC 0031 session / ISSUE-0085 epoch). The
         §F identity rule constrains user_id only (never typed); session and
         epoch are operator-namespace ids the panel reads back via the bindings. -->
    <ScopeSelector bind:sessionId bind:epochId {sending} />
    <span class="composer-hint" aria-hidden="true"
      ><kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line</span
    >
    {#if length >= COUNTER_FROM}
      <span class="char-count" class:over={length > MAX_MESSAGE_LENGTH}>{length} / {MAX_MESSAGE_LENGTH}</span>
    {/if}
    <button type="submit" disabled={!canSend || !chattable}>
      <Icon name="send" size={14} />{sending ? "Sending…" : "Send"}
    </button>
  </div>
</form>
