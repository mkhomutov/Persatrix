<script>
  // The marker for a hole in the timeline (ISSUE-0222): messages that arrived
  // between two polls and were not all read, because the feed stops after a
  // fixed number of pages or a page failed. It is drawn over the row just
  // below the hole, inside that row's list item (as the day divider is), so
  // the timeline's items stay one per message. The button reads the next
  // pages of the span; lib/backfill.svelte.js holds the hole's state.
  import Icon from "../ui/Icon.svelte";

  // gap — { loading, error } of the hole; onLoad — read more of it.
  let { gap, onLoad } = $props();
</script>

<div class="timeline-gap">
  <p class="gap-note">Some messages here are not loaded yet.</p>
  {#if gap.error}
    <p class="poll-error" role="status">{gap.error}</p>
  {/if}
  <button type="button" class="btn-sm btn-ghost" onclick={onLoad} disabled={gap.loading}>
    <Icon name="arrow-down" size={14} />{gap.loading ? "Loading missing messages…" : "Load missing messages"}
  </button>
</div>

<style>
  .timeline-gap {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    margin: 0.5rem 0.75rem;
    padding: 0.5rem 0;
    border-block: 1px dashed var(--border-strong, #cfd4dd);
  }

  .gap-note {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-muted, #5d6576);
  }
</style>
