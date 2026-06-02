<script>
  // Channel-timeline panel slot (RFC 0048 Phase 1). PR 5 replaces this with the
  // channel list + newest-first history that stays live by polling
  // (visibility-pause + error-backoff + head-poll de-dupe), plus the optional
  // human publish into a group channel.
  let { userId } = $props();
</script>

<section class="panel" aria-label="Channels">
  <h2>Channels</h2>
  <p class="placeholder">
    The channel timeline arrives in RFC 0048 Phase 1 PR 5. You will watch a
    channel and optionally post into it here.
  </p>
  <p class="identity">Acting as <code>{userId}</code></p>
</section>
