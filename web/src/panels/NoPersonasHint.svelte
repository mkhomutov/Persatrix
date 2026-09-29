<script>
  // Shown where the sidebar's persona list would be when NO personas are
  // registered but channels exist — so the DM entry point does not vanish
  // SILENTLY (the {:else} of ConversationRail's persona-list guard). Its own
  // component, mirroring PublishComposer / OnboardingEmpty.
  //
  // The empty list's most common cause is a cloud demo whose agents fail
  // closed at startup on missing provider config (RFC 0053 §C — e.g. an unfilled
  // watsonx project_id), leaving nothing registered to DM. The orchestrator has
  // no expected-agent roster (agents self-register), so this reports the
  // observed state, not a fabricated "N failed to register" count.
  //
  // onRefresh — re-check the persona list without a reload (the panel's
  //   loadAgents), so a late-registering agent becomes pickable — the same
  //   no-reload affordance as the OnboardingEmpty on-ramp.
  let { onRefresh } = $props();
</script>

<p class="no-personas" role="status">
  No personas are registered, so direct messages aren't available. If you just
  started a cloud demo, an agent may have failed to start on missing provider
  config — check <code>docker compose logs</code>.
  <!-- "Refresh personas", not a bare "Refresh": the sidebar's toolbar already
       owns "Refresh" (re-list personas and channels) in this same panel, so a
       distinct label keeps both the operator and the a11y tree unambiguous. -->
  <button type="button" class="retry" onclick={onRefresh}>Refresh personas</button>
</p>
