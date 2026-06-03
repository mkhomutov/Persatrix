<script>
  // Shared onboarding empty-state (RFC 0048 amendment §F): turns a bare
  // dead-end line into an on-ramp — a title, caller-supplied guidance, a
  // no-reload re-check, and a link to the web-console quick-start. Both hero
  // panels (Chat, ChannelTimeline) render it, so the DOCS_URL and the markup
  // live in one place rather than being duplicated per panel.
  //
  // DOCS_URL is an absolute GitHub URL: the console is served at /ui, but the
  // docs are not served by the orchestrator.
  const DOCS_URL =
    "https://github.com/mkhomutov/Persatrix/blob/main/docs/guides/web-console.md";

  // title  — the headline line ("No personas are registered yet.").
  // onRetry — re-check handler wired to the panel's loader (no full reload).
  // children — the guidance paragraph (may contain inline markup like <code>).
  let { title, onRetry, children } = $props();
</script>

<div class="empty onboarding">
  <p>{title}</p>
  <p>{@render children()}</p>
  <p>
    <button type="button" class="retry" onclick={onRetry}>Refresh</button>
    <a href={DOCS_URL} target="_blank" rel="noopener noreferrer"
      >Web console quick-start ↗</a
    >
  </p>
</div>
