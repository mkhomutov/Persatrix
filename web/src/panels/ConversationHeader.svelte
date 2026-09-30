<script>
  // The conversation column's header — a persona face for a DM (PersonaHeader
  // plus the resolve status lines and the way back out), or the watched group
  // channel's identity (#name, description, who is in it). It also carries the
  // layout controls: the conversation-list drawer toggle (narrow screens) and
  // the management-rail toggle.
  //
  // isDM — which header to draw.
  // personaInfo / dmResolving / dmResolveError / sending / onExit — the DM half.
  // channelInfo / members / agentsById — the group channel half.
  // navOpen / onToggleNav — the conversation-list drawer.
  // showDetailsToggle / detailsOpen / onToggleDetails — the management rail.
  import PersonaHeader from "./PersonaHeader.svelte";
  import Icon from "../ui/Icon.svelte";
  import Avatar from "../ui/Avatar.svelte";
  import { channelLabel } from "../lib/format.js";

  let {
    isDM = false,
    personaInfo = null,
    dmResolving = false,
    dmResolveError = "",
    sending = false,
    onExit,
    channelInfo = null,
    members = [],
    agentsById = {},
    navOpen = false,
    onToggleNav,
    showDetailsToggle = false,
    detailsOpen = false,
    onToggleDetails,
  } = $props();

  const memberCount = $derived(members.length);
  // A few faces for the header's member stack; the count carries the rest.
  const faces = $derived(members.slice(0, 4));
</script>

<header class="convo-bar">
  <button
    type="button"
    class="btn-icon btn-ghost nav-toggle"
    aria-label="Show conversations"
    aria-expanded={navOpen}
    onclick={onToggleNav}
  >
    <Icon name="menu" size={18} />
  </button>

  {#if isDM}
    <PersonaHeader info={personaInfo} />
    {#if personaInfo}
      <!-- Leaves the conversation for the group view — the web analogue of
           quitting the CLI chat REPL. Locked during a turn so it can't strand
           an in-flight reply. -->
      <button type="button" class="btn-sm exit-chat" onclick={onExit} disabled={sending}>
        <Icon name="x" size={14} />Exit conversation
      </button>
    {/if}
  {:else if channelInfo}
    <div class="convo-heading">
      <span class="convo-icon" aria-hidden="true"><Icon name="hash" size={18} /></span>
      <div class="convo-text">
        <h2 class="convo-title">{channelLabel(channelInfo)}</h2>
        <p class="convo-sub">
          {#if channelInfo.description}<span class="convo-desc">{channelInfo.description}</span>{/if}
          <span>{memberCount} member{memberCount === 1 ? "" : "s"}</span>
        </p>
      </div>
    </div>
    {#if faces.length > 0}
      <span class="face-stack" aria-hidden="true">
        {#each faces as member (member.id)}
          <Avatar id={member.id} label={agentsById[member.id]?.name || member.id} size={24} />
        {/each}
        {#if memberCount > faces.length}<span class="face-more">+{memberCount - faces.length}</span>{/if}
      </span>
    {/if}
  {/if}

  {#if showDetailsToggle}
    <button
      type="button"
      class="btn-icon btn-ghost details-toggle"
      class:active={detailsOpen}
      aria-label="Channel details"
      aria-expanded={detailsOpen}
      title={detailsOpen ? "Hide channel details" : "Show channel details"}
      onclick={onToggleDetails}
    >
      <Icon name="panel-right" size={18} />
    </button>
  {/if}
</header>

{#if isDM && dmResolving}
  <p class="loading convo-status" role="status">Opening conversation…</p>
{/if}
{#if isDM && dmResolveError}
  <p class="poll-error convo-status" role="status">{dmResolveError}</p>
{/if}
