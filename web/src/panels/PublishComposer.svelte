<script>
  // Optional human publish (extracted from ChannelTimeline.svelte to keep the
  // panel under the review-size cap): a clearly-labelled write action so it
  // reads as deliberate, not a search box. The sender is the /ui/context
  // principal — never a free-text field (RFC §F rule 1).
  //
  // content — bound to the panel's draft text.
  // publishing — true while a post is in flight (disables the input).
  // canPublish — whether the Post button is enabled.
  // onSubmit / onKeydown — form submit + textarea keydown handlers.
  let {
    content = $bindable(),
    publishing,
    canPublish,
    onSubmit,
    onKeydown,
  } = $props();
</script>

<form class="publish" onsubmit={onSubmit}>
  <label>
    Message
    <textarea
      bind:value={content}
      rows="2"
      placeholder="Post a message to this channel… (Enter to post, Shift+Enter for a new line)"
      disabled={publishing}
      onkeydown={onKeydown}
    ></textarea>
  </label>
  <button type="submit" disabled={!canPublish}>
    {publishing ? "Posting…" : "Post"}
  </button>
</form>
