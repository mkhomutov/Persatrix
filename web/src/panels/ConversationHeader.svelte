<script>
  // The conversation column's header — a persona face for a DM (PersonaHeader
  // plus the resolve status lines), or the watched group channel's identity
  // (#name, description, member count). Extracted from ChannelTimeline.svelte
  // to keep the panel under the review-size cap.
  //
  // isDM — which header to draw.
  // personaInfo / dmResolving / dmResolveError — the DM half's state.
  // channelInfo — the watched group channel's record (null = no header).
  // memberCount — the group channel's roster size.
  import PersonaHeader from "./PersonaHeader.svelte";
  import { channelLabel } from "../lib/format.js";

  let {
    isDM = false,
    personaInfo = null,
    dmResolving = false,
    dmResolveError = "",
    channelInfo = null,
    memberCount = 0,
  } = $props();
</script>

{#if isDM}
  <PersonaHeader info={personaInfo} />
  {#if dmResolving}
    <p class="loading convo-status" role="status">Opening conversation…</p>
  {/if}
  {#if dmResolveError}
    <p class="poll-error convo-status" role="status">{dmResolveError}</p>
  {/if}
{:else if channelInfo}
  <header class="convo-header">
    <h2 class="convo-title">
      <span class="hash" aria-hidden="true">#</span>{channelLabel(channelInfo)}
    </h2>
    {#if channelInfo.description}
      <span class="convo-sub">{channelInfo.description}</span>
    {/if}
    <span class="convo-sub"
      >{memberCount} member{memberCount === 1 ? "" : "s"}</span
    >
  </header>
{/if}
