<script>
  // Channels panel — the console's single conversation surface (RFC 0048
  // chat-panel-retirement amendment §B): group channels (watch + publish) and DMs
  // (talk over the synchronous chat façade). A chat IS a `dm:` channel server-side
  // (RFC 0011 chat-as-DM), so both render + poll through the shared
  // ConversationFeed; the DM-specific lifecycle (open/resolve/send/cancel/exit)
  // lives in lib/dmSession.svelte.js. userId is the /ui/context principal (RFC
  // §F) or App.svelte's "acting as" override (slice1-ux §E). Pure render-over-API.
  //
  // Layout: sidebar (conversation lists) | conversation | management rail. The
  // sidebar becomes a drawer on narrow screens (navOpen) and the management rail
  // can be folded away (detailsOpen, remembered per browser).
  import { tick, untrack } from "svelte";
  import {
    listAgents,
    listChannels,
    publishMessage,
    ApiError,
  } from "../lib/api.js";
  import { isDMChannel, channelLabel } from "../lib/format.js";
  import { isChattable } from "../lib/agents.js";
  import { buildPublishPayload } from "../lib/mentions.js";
  import { selection } from "../lib/selection.svelte.js";
  import { DmSession } from "../lib/dmSession.svelte.js";
  import { byLabel } from "../lib/filter.js";
  import { readPref, writePref } from "../lib/prefs.js";
  import OnboardingEmpty from "./OnboardingEmpty.svelte";
  import PublishComposer from "./PublishComposer.svelte";
  import DmComposer from "./DmComposer.svelte";
  import CreateChannelForm from "./CreateChannelForm.svelte";
  import ConversationFeed from "./ConversationFeed.svelte";
  import ConversationRail from "./ConversationRail.svelte";
  import ConversationHeader from "./ConversationHeader.svelte";
  import ManagementRail from "./ManagementRail.svelte";
  import Icon from "../ui/Icon.svelte";

  // canCreate / canConfigEdit: the create (amendment §A) + RFC 0050 config-edit
  // capabilities, each reduced to enabled && available by the shell.
  let { userId, canCreate = false, canConfigEdit = false } = $props();

  let agents = $state([]);
  let agentsById = $state({});
  let agentsLoaded = $state(false);

  let channels = $state([]);
  let channelsError = $state("");
  let channelsLoaded = $state(false);
  // A retry after a failed load keeps the error card up (its button busy)
  // until the answer arrives, rather than flashing an empty workspace.
  let channelsRetrying = $state(false);
  // selectedChannel is the GROUP channel being watched; it persists across a DM
  // overlay so leaving the DM returns to it.
  let selectedChannel = $state("");
  let publishContent = $state("");
  let publishing = $state(false);
  let publishError = $state("");

  let showCreateForm = $state(false);
  // pendingSelectId lands the operator in a just-created group channel — a
  // one-shot in-panel hand-off replacing the removed cross-panel nav (§C).
  let pendingSelectId = "";
  // Unsent drafts per group channel (this page load): switching channels parks
  // the draft and brings back the next channel's own, so a half-written post
  // never lands in the wrong room. DM drafts live in the DmSession.
  const channelDrafts = new Map();

  // watchChannel switches the watched group channel, swapping the drafts.
  function watchChannel(id) {
    if (id !== selectedChannel) {
      if (selectedChannel) channelDrafts.set(selectedChannel, publishContent);
      publishContent = channelDrafts.get(id) ?? "";
    }
    selectedChannel = id;
  }

  // Remember the watched channel so a reload returns to it.
  $effect(() => {
    if (selectedChannel) writePref("lastChannel", selectedChannel);
  });

  // feed is the ConversationFeed handle — echo()/pollNow() surface a write, and
  // markThinking()/clearThinking() drive the optimistic half of its
  // live-presence indicator from the send/publish seams below (the feed's own
  // /activity poll owns the authoritative half).
  let feed = $state(null);

  // Layout: the sidebar as a drawer on narrow screens, and the foldable
  // management rail. Where the rail fits beside the conversation it opens by
  // default and remembers being folded; on a narrow screen it overlays the
  // conversation, so it always starts closed there.
  // (Wide starts one pixel past the CSS overlay breakpoint, max-width 1180px.)
  const wideLayout = () =>
    globalThis.matchMedia?.("(min-width: 1181px)").matches ?? true;
  // The sidebar is a drawer only below this width (styles/responsive.css).
  const drawerLayout = () =>
    globalThis.matchMedia?.("(max-width: 860px)").matches ?? false;
  let navOpen = $state(false);
  let detailsOpen = $state(wideLayout() ? readPref("detailsOpen", true) : false);

  function setDetailsOpen(open) {
    detailsOpen = open;
    if (wideLayout()) writePref("detailsOpen", open);
  }

  const dm = new DmSession({
    userId: () => userId,
    feed: () => feed,
    persona: (id) => agentsById[id] ?? null,
  });

  const isDM = $derived(Boolean(dm.agent));
  // activeChannel is the id the feed renders: the resolved DM in DM mode, else the
  // group channel. A fresh DM is "" until its first send creates the channel.
  const activeChannel = $derived(isDM ? dm.channelId : selectedChannel);
  // DMs are filtered OUT of the channel list — reached via the persona entry
  // point, never as a raw `dm:` row (amendment §B). Sorted by name so a long
  // list reads alphabetically; the default watched channel is the first row.
  const groupChannels = $derived(
    channels.filter((c) => !isDMChannel(c)).sort(byLabel(channelLabel)),
  );
  // The watched group channel's record — drives the conversation header.
  const selectedChannelInfo = $derived(
    groupChannels.find((c) => c.id === selectedChannel) ?? null,
  );
  // Members of the watched channel — `@`-mention source + resolve set (RFC 0011).
  const selectedChannelMembers = $derived(selectedChannelInfo?.members ?? []);
  // The management rail (members + settings) exists only for a watched group
  // channel with at least one management capability (see ManagementRail).
  const hasDetailsRail = $derived(
    Boolean(selectedChannel) && !isDM && (canCreate || canConfigEdit),
  );

  const selectedAgentInfo = $derived(agentsById[dm.agent] ?? null);
  const selectedAgentChattable = $derived(isChattable(selectedAgentInfo));

  const canPublish = $derived(
    Boolean(selectedChannel) &&
      !isDM &&
      publishContent.trim().length > 0 &&
      !publishing,
  );
  const canSend = $derived(
    isDM && dm.message.trim().length > 0 && !dm.sending && selectedAgentChattable,
  );

  // bothEmpty drives the merged onboarding (§D): only a stack with NO personas
  // AND NO channels is a true dead end — either alone is an entry point.
  const bothEmpty = $derived(
    agentsLoaded &&
      channelsLoaded &&
      agents.length === 0 &&
      channels.length === 0,
  );

  // loadAgents fetches the persona list (DM entry point + decoration; non-fatal —
  // a failure just empties the list). It also resumes a deliberately-opened DM
  // across a tab unmount (§B sticky rehome): only an explicit remembered id
  // reopens one, so the default view is the group timeline, not an auto-DM. A
  // re-check keeps the workspace mounted; only the first load shows the loader.
  function loadAgents() {
    return listAgents()
      .then((list) => {
        agents = list;
        agentsById = Object.fromEntries(list.map((a) => [a.id, a]));
        const remembered = selection.dmAgent;
        if (
          remembered &&
          !dm.agent &&
          list.some((a) => a.id === remembered && isChattable(a))
        ) {
          dm.open(remembered);
        }
      })
      .catch(() => {})
      .finally(() => {
        agentsLoaded = true;
      });
  }

  function loadChannels() {
    // A retry keeps the error card (and its busy button) until the answer.
    channelsRetrying = Boolean(channelsError);
    return listChannels()
      .then((result) => {
        channelsError = "";
        channels = result.channels ?? [];
        // One-shot create hand-off (§C): land in the just-created channel
        // (exiting any DM); else default to the channel watched last time, or
        // the first GROUP channel, but never yank an operator out of an open DM.
        const requested = pendingSelectId;
        pendingSelectId = "";
        if (requested && channels.some((c) => c.id === requested)) {
          if (dm.agent) dm.exit();
          watchChannel(requested);
        } else if (groupChannels.length > 0 && !selectedChannel && !isDM) {
          const last = readPref("lastChannel", "");
          watchChannel(
            groupChannels.some((c) => c.id === last) ? last : groupChannels[0].id,
          );
        }
      })
      .catch((err) => {
        channelsError = `Could not load channels: ${err.message}`;
      })
      .finally(() => {
        channelsRetrying = false;
        channelsLoaded = true;
      });
  }

  function refreshAll() {
    loadAgents();
    loadChannels();
  }

  // Load once on mount. untrack: the loaders read panel state (the retry reads
  // channelsError), and that must not turn these into re-running effects.
  $effect(() => {
    untrack(loadAgents);
  });

  $effect(() => {
    untrack(loadChannels);
  });

  async function publish() {
    if (publishing) {
      return;
    }
    const content = publishContent.trim();
    if (!selectedChannel || isDM || content.length === 0) {
      return;
    }
    // Capture the target: the list stays enabled during a publish, so a switch
    // mid-flight must not echo into the now-current conversation.
    const target = selectedChannel;
    // Lift `@id` tokens resolving to a member of THIS channel (RFC 0011).
    const payload = buildPublishPayload(userId, content, selectedChannelMembers);
    publishError = "";
    publishing = true;
    try {
      const stored = await publishMessage(target, payload);
      // Superseded by a switch (channel or into a DM): drop the echo — the
      // message persisted and surfaces on its own conversation's poll — and
      // the draft parked for that channel, which is this very post.
      if (isDM || selectedChannel !== target) {
        channelDrafts.delete(target);
        if (isDM && selectedChannel === target) publishContent = "";
        return;
      }
      feed?.echo(stored);
      publishContent = "";
      // Light the indicator for the agents this post @-addressed (the expected
      // responders); a broadcast that names nobody shows nothing, not a guess.
      feed?.markThinking((payload.mentions ?? []).filter((id) => id !== userId && agentsById[id]));
    } catch (err) {
      if (isDM || selectedChannel !== target) {
        return;
      }
      publishError =
        err instanceof ApiError
          ? err.message
          : `The message could not be posted: ${err.message}`;
    } finally {
      publishing = false;
    }
  }

  function onPublishSubmit(event) {
    event.preventDefault();
    publish();
  }

  // Enter posts / sends; Shift+Enter keeps a newline; an IME composition's
  // Enter confirms the composition instead.
  function enterSubmits(event, action) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      action();
    }
  }

  function onDmSubmit(event) {
    event.preventDefault();
    dm.send();
  }

  // Picking a group channel is an intent to watch it — even the one watched
  // before a DM: leave any DM and drop a stale publish error.
  function onPickChannel(id) {
    publishError = "";
    if (dm.agent) {
      dm.exit();
    }
    watchChannel(id);
    closeDrawerAfterPick();
  }

  function onPickPersona(id) {
    closeDrawerAfterPick();
    if (dm.sending || id === dm.agent) {
      return;
    }
    dm.open(id);
  }

  // A pick from the drawer closes it and hands focus to the conversation it
  // opened — the drawer, and the row that had focus, slide out of reach.
  let conversationEl = $state(null);
  function closeDrawerAfterPick() {
    if (!navOpen) return;
    navOpen = false;
    tick().then(() => conversationEl?.focus());
  }

  // onChannelCreated lands the operator in the just-created group channel via the
  // one-shot pendingSelectId hand-off (§C). (DMs are started from the persona
  // entry point, not the create form — so there's no direct-mode result here.)
  function onChannelCreated(channel) {
    showCreateForm = false;
    pendingSelectId = channel?.id ?? "";
    loadChannels();
  }
</script>

<section class="panel channels" aria-label="Channels">
  {#if channelsError}
    <div class="boot-card is-error">
      <div class="boot-icon"><Icon name="alert" size={20} /></div>
      <p class="boot error" role="alert">{channelsError}</p>
      <button type="button" class="retry" onclick={loadChannels} disabled={channelsRetrying}>
        {channelsRetrying ? "Retrying…" : "Retry"}
      </button>
    </div>
  {:else if !channelsLoaded || !agentsLoaded}
    <div class="workspace-skeleton" aria-hidden="true">
      <div class="sk-rail">
        {#each [70, 55, 62, 48, 66, 58] as w}<span class="skeleton" style="width: {w}%"></span>{/each}
      </div>
      <div class="sk-feed">
        {#each [42, 68, 54, 72, 38] as w}<span class="skeleton" style="width: {w}%"></span>{/each}
      </div>
    </div>
    <p class="sr-only" role="status">Loading…</p>
  {:else if bothEmpty}
    <!-- Merged onboarding (§D): only a stack with neither personas nor channels
         is a dead end. One first-contact surface for both entry points. -->
    <OnboardingEmpty title="No personas or channels yet." onRetry={refreshAll}>
      Register a persona with <code>persatrix agent register</code> to start a
      DM, or define group channels in <code>config/channels.yaml</code>, then
      re-check.
    </OnboardingEmpty>
  {:else}
    <div class="workspace" class:with-details={hasDetailsRail && detailsOpen}>
      <ConversationRail
        {groupChannels}
        {agents}
        {selectedChannel}
        selectedAgent={dm.agent}
        {isDM}
        sending={dm.sending}
        {canCreate}
        {userId}
        open={navOpen}
        {onPickChannel}
        {onPickPersona}
        onRefresh={refreshAll}
        onRefreshAgents={loadAgents}
        onNewChannel={() => (showCreateForm = true)}
        onRequestOpen={() => (navOpen = drawerLayout())}
        onClose={() => (navOpen = false)}
      />
      {#if navOpen}
        <button type="button" class="drawer-scrim" aria-label="Close the conversation list" tabindex="-1" onclick={() => (navOpen = false)}></button>
      {/if}

      <!-- Conversation column: header, scrolling feed, docked composer. -->
      <section class="conversation" aria-label="Conversation" tabindex="-1" bind:this={conversationEl}>
        <ConversationHeader
          {isDM}
          personaInfo={selectedAgentInfo}
          dmResolving={dm.resolving}
          dmResolveError={dm.resolveError}
          sending={dm.sending}
          onExit={() => dm.exit()}
          channelInfo={selectedChannelInfo}
          members={selectedChannelMembers}
          {agentsById}
          {navOpen}
          onToggleNav={() => (navOpen = !navOpen)}
          showDetailsToggle={hasDetailsRail}
          {detailsOpen}
          onToggleDetails={() => setDetailsOpen(!detailsOpen)}
        />

        {#if !activeChannel && !isDM}
          <!-- Neutral default: name both ways in (only reachable when at least
               one entry point exists — bothEmpty is handled above). -->
          <div class="empty-state lobby">
            <div class="empty-icon"><Icon name="message" size={22} /></div>
            <p class="empty">Select a persona to direct-message, or a channel to watch.</p>
          </div>
        {:else}
          <div class="feed-area">
            <ConversationFeed bind:this={feed} channelId={activeChannel} {userId} {agentsById} {isDM} peerId={dm.agent} members={selectedChannelMembers} onCancelTurn={isDM && dm.sending ? () => dm.cancel() : null} />
          </div>
        {/if}

        <div class="composer-dock">
          {#if isDM}
            {#if dm.sendError}
              <p class="boot error" role="alert">{dm.sendError}</p>
            {/if}
            <DmComposer
              bind:message={dm.message}
              bind:sessionId={dm.sessionId}
              bind:epochId={dm.epochId}
              sending={dm.sending}
              {canSend}
              chattable={selectedAgentChattable}
              hasPersona={Boolean(selectedAgentInfo)}
              personaName={selectedAgentInfo?.name || dm.agent}
              onSubmit={onDmSubmit}
              onKeydown={(event) => enterSubmits(event, () => dm.send())}
            />
          {:else if selectedChannel}
            {#if publishError}
              <p class="boot error" role="alert">{publishError}</p>
            {/if}
            <PublishComposer
              bind:content={publishContent}
              {publishing}
              {canPublish}
              {userId}
              {agentsById}
              members={selectedChannelMembers}
              channelName={selectedChannelInfo ? channelLabel(selectedChannelInfo) : ""}
              onSubmit={onPublishSubmit}
              onKeydown={(event) => enterSubmits(event, publish)}
            />
          {/if}
        </div>
      </section>

      {#if hasDetailsRail}
        <ManagementRail hidden={!detailsOpen} channelId={selectedChannel} members={selectedChannelMembers} {agents} {agentsById} {userId} {canCreate} {canConfigEdit} onChanged={loadChannels} onClose={() => setDetailsOpen(false)} />
      {/if}
    </div>

    {#if canCreate && showCreateForm}
      <CreateChannelForm {agents} {userId} onCreated={onChannelCreated} onCancel={() => (showCreateForm = false)} />
    {/if}
  {/if}

  {#if channelsError || !channelsLoaded || !agentsLoaded || bothEmpty}
    <!-- Outside workspace mode the rail (which carries the identity) isn't
         rendered, but the effective identity must stay visible in every state
         (§E/§F — the panel always echoes who it acts as). -->
    <p class="identity boot-identity">Acting as <code>{userId}</code></p>
  {/if}
</section>
