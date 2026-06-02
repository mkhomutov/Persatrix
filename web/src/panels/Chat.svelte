<script>
  // Chat panel slot (RFC 0048 Phase 1). PR 4 replaces this placeholder with the
  // persona picker + synchronous chat over POST /api/v1/agents/{id}/chat. The
  // shell already passes the context-derived userId in (the RFC §F single
  // identity source) so PR 4 consumes it without re-plumbing identity.
  let { userId } = $props();
</script>

<section class="panel" aria-label="Chat">
  <h2>Chat</h2>
  <p class="placeholder">
    The chat panel arrives in RFC 0048 Phase 1 PR 4. You will pick a persona and
    talk to it here.
  </p>
  <p class="identity">Acting as <code>{userId}</code></p>
</section>
