<script>
  // Channels panel — the console's single conversation surface (RFC 0048
  // chat-panel-retirement amendment §B): group channels (watch + publish) and DMs
  // (talk over the synchronous chat façade). A chat IS a `dm:` channel server-side
  // (RFC 0011 chat-as-DM), so both render + poll through the shared
  // ConversationFeed; the DM-specific bits are the send path (sendChat vs
  // publishMessage) + a persona header. userId is the /ui/context principal (RFC
  // §F), never prompted. Pure render-over-API.
  import {
    listAgents,
    listChannels,
    publishMessage,
    getChatHistory,
    sendChat,
    ApiError,
  } from "../lib/api.js";
  import { isDMChannel } from "../lib/format.js";
  import { isChattable } from "../lib/agents.js";
  import { buildPublishPayload } from "../lib/mentions.js";
  import { selection } from "../lib/selection.svelte.js";
  import OnboardingEmpty from "./OnboardingEmpty.svelte";
  import PublishComposer from "./PublishComposer.svelte";
  import DmComposer from "./DmComposer.svelte";
  import CreateChannelForm from "./CreateChannelForm.svelte";
  import ChannelMembers from "./ChannelMembers.svelte";
  import ChannelSettings from "./ChannelSettings.svelte";
  import ChannelPicker from "./ChannelPicker.svelte";
  import ConversationFeed from "./ConversationFeed.svelte";
  import PersonaPicker from "./PersonaPicker.svelte";
  import PersonaHeader from "./PersonaHeader.svelte";

  // canCreate / canConfigEdit: the create (amendment §A) + RFC 0050 config-edit
  // capabilities, each reduced to enabled && available by the shell. config-edit
  // is threaded here in PR 1 (plumbing); nested ChannelSettings consumes it in PR 2.
  let { userId, canCreate = false, canConfigEdit = false } = $props();

  // Mirrors the server's rune limit (chat_handler.go) for immediate feedback;
  // counts code points, not UTF-16 units. The server still enforces it.
  const MAX_MESSAGE_LENGTH = 4000;

  let agents = $state([]);
  let agentsById = $state({});
  let agentsLoaded = $state(false);
  let selectedAgent = $state(""); // DM persona, "" = group mode
  let dmChannelId = $state(""); // resolved DM channel, "" until first send
  let dmResolving = $state(false);
  let dmResolveError = $state("");
  let dmToken = 0;

  let message = $state(""); // DM draft (abortable synchronous turn)
  let sessionId = $state("");
  let epochId = $state("");
  let sending = $state(false);
  let sendError = $state("");
  let chatController = null;

  let channels = $state([]);
  let channelsError = $state("");
  let channelsLoaded = $state(false);
  // selectedChannel is the GROUP channel being watched; it persists across a DM
  // overlay so Exit returns to it.
  let selectedChannel = $state("");
  let publishContent = $state("");
  let publishing = $state(false);
  let publishError = $state("");

  let showCreateForm = $state(false);
  // pendingSelectId lands the operator in a just-created group channel — a
  // one-shot in-panel hand-off replacing the removed cross-panel nav (§C).
  let pendingSelectId = "";

  // feed is the ConversationFeed handle — echo()/pollNow() surface a write, and
  // markThinking()/clearThinking() drive the optimistic half of its
  // live-presence indicator from the send/publish seams below (the feed's own
  // /activity poll owns the authoritative half).
  let feed = $state(null);

  const isDM = $derived(Boolean(selectedAgent));
  // activeChannel is the id the feed renders: the resolved DM in DM mode, else the
  // group channel. A fresh DM is "" until its first send creates the channel.
  const activeChannel = $derived(isDM ? dmChannelId : selectedChannel);
  // DMs are filtered OUT of the channel picker — reached via the persona entry
  // point, never as a raw `dm:` row (amendment §B).
  const groupChannels = $derived(channels.filter((c) => !isDMChannel(c)));
  // Members of the watched channel — `@`-mention source + resolve set (RFC 0011).
  const selectedChannelMembers = $derived(
    groupChannels.find((c) => c.id === selectedChannel)?.members ?? []);

  const selectedAgentInfo = $derived(
    agents.find((agent) => agent.id === selectedAgent) ?? null,
  );
  const selectedAgentChattable = $derived(isChattable(selectedAgentInfo));

  const canPublish = $derived(
    Boolean(selectedChannel) &&
      !isDM &&
      publishContent.trim().length > 0 &&
      !publishing,
  );
  const canSend = $derived(
    Boolean(selectedAgent) &&
      message.trim().length > 0 &&
      !sending &&
      selectedAgentChattable,
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
  // a failure just empties the picker). It also resumes a deliberately-opened DM
  // across a tab unmount (§B sticky rehome): only an explicit remembered id
  // reopens one, so the default view is the group timeline, not an auto-DM.
  function loadAgents() {
    agentsLoaded = false;
    return listAgents()
      .then((list) => {
        agents = list;
        agentsById = Object.fromEntries(list.map((a) => [a.id, a]));
        const remembered = selection.dmAgent;
        if (
          remembered &&
          !selectedAgent &&
          list.some((a) => a.id === remembered && isChattable(a))
        ) {
          openDM(remembered);
        }
      })
      .catch(() => {})
      .finally(() => {
        agentsLoaded = true;
      });
  }

  // resolveDM reads the persona's canonical DM channel id WITHOUT creating it (a
  // never-messaged persona returns 200-empty, not 404 — the read-only LookupDM
  // path, slice1-ux §B). The id drives the feed via activeChannel.
  function resolveDM(agentId) {
    const token = ++dmToken;
    dmResolving = true;
    dmResolveError = "";
    return getChatHistory(agentId, { userId })
      .then((result) => {
        if (token !== dmToken) return;
        const msgs = result.messages ?? [];
        dmChannelId = msgs.length > 0 ? (msgs[0].channel_id ?? "") : "";
      })
      .catch((err) => {
        if (token !== dmToken) return;
        dmResolveError = `Could not open the conversation: ${err.message}`;
        dmChannelId = "";
      })
      .finally(() => {
        if (token === dmToken) dmResolving = false;
      });
  }

  // openDM enters DM mode for a persona and resolves its channel; records the
  // sticky selection so a tab round-trip resumes it. The group selection is left
  // intact — the context Exit returns to.
  function openDM(agentId) {
    if (!agentId) return;
    selection.dmAgent = agentId;
    selectedAgent = agentId;
    dmChannelId = "";
    message = "";
    sendError = "";
    dmResolveError = "";
    return resolveDM(agentId);
  }

  function onPersonaPick() {
    openDM(selectedAgent);
  }

  // exitDM leaves the DM for the group view; the null sentinel records a
  // deliberate exit so a tab switch doesn't auto-reopen it.
  function exitDM() {
    selection.dmAgent = null;
    selectedAgent = "";
    dmChannelId = "";
    dmResolveError = "";
    message = "";
    sendError = "";
  }

  function loadChannels() {
    channelsError = "";
    channelsLoaded = false;
    return listChannels()
      .then((result) => {
        channels = result.channels ?? [];
        // One-shot create hand-off (§C): land in the just-created channel
        // (exiting any DM); else default to the first GROUP channel, but never
        // yank an operator out of an open DM.
        const requested = pendingSelectId;
        pendingSelectId = "";
        const groups = channels.filter((c) => !isDMChannel(c));
        if (requested && channels.some((c) => c.id === requested)) {
          if (selectedAgent) exitDM();
          selectedChannel = requested;
        } else if (groups.length > 0 && !selectedChannel && !isDM) {
          selectedChannel = groups[0].id;
        }
      })
      .catch((err) => {
        channelsError = `Could not load channels: ${err.message}`;
      })
      .finally(() => {
        channelsLoaded = true;
      });
  }

  $effect(() => {
    loadAgents();
  });

  $effect(() => {
    loadChannels();
  });

  async function publish() {
    if (publishing) {
      return;
    }
    const content = publishContent.trim();
    if (!selectedChannel || isDM || content.length === 0) {
      return;
    }
    // Capture the target: the picker stays enabled during a publish, so a switch
    // mid-flight must not echo into the now-current conversation.
    const target = selectedChannel;
    // Lift `@id` tokens resolving to a member of THIS channel (RFC 0011).
    const payload = buildPublishPayload(userId, content, selectedChannelMembers);
    publishError = "";
    publishing = true;
    try {
      const stored = await publishMessage(target, payload);
      // Superseded by a switch (channel or into a DM): drop the echo — the
      // message persisted and surfaces on its own conversation's poll.
      if (isDM || selectedChannel !== target) {
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

  function onPublishKeydown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      publish();
    }
  }

  // send issues one synchronous turn and refreshes from the persisted channel: an
  // existing DM polls its head (surfacing the stored user turn + reply); a fresh
  // DM re-resolves its now-created channel id, driving the feed to load + poll.
  // The persisted messages are the single source of truth — no local echo, so the
  // poll can never double-show the turn.
  async function send() {
    if (sending) {
      return;
    }
    sendError = "";
    const text = message.trim();
    if (!selectedAgent || text.length === 0) {
      return;
    }
    // Enter-to-send bypasses the disabled button, so re-check chattability.
    if (!isChattable(selectedAgentInfo)) {
      return;
    }
    if ([...text].length > MAX_MESSAGE_LENGTH) {
      sendError = `Message exceeds the maximum length of ${MAX_MESSAGE_LENGTH} characters.`;
      return;
    }

    const agentAtSend = selectedAgent;
    const hadChannel = Boolean(dmChannelId);
    sending = true;
    chatController = new AbortController();
    // The synchronous turn IS the DM's "thinking" signal (cleared below/finally).
    feed?.markThinking([agentAtSend]);
    try {
      const payload = { message: text, userId, signal: chatController.signal };
      const usedSession = sessionId.trim();
      const usedEpoch = epochId.trim();
      if (usedSession) payload.sessionId = usedSession;
      if (usedEpoch) payload.epochId = usedEpoch;
      await sendChat(agentAtSend, payload);
      // Switched persona mid-turn: the reply belongs to a DM already left.
      if (agentAtSend !== selectedAgent) {
        return;
      }
      message = "";
      feed?.clearThinking([agentAtSend], { replied: true });
      if (hadChannel) {
        feed?.pollNow();
      } else {
        resolveDM(agentAtSend);
      }
    } catch (err) {
      // A user cancel surfaces as an AbortError (status-0 ApiError wrapping it):
      // not a failure — drop it silently rather than alarming over a own cancel.
      if (chatController?.signal.aborted || err?.cause?.name === "AbortError") {
        // intentionally no sendError
      } else {
        sendError =
          err instanceof ApiError
            ? err.message
            : `The message could not be sent: ${err.message}`;
      }
    } finally {
      sending = false;
      chatController = null;
      // Backstop for cancel/error/mid-turn switch — clears with no idle flash.
      feed?.clearThinking([agentAtSend]);
    }
  }

  function onDmSubmit(event) {
    event.preventDefault();
    send();
  }

  function onDmKeydown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  }

  function cancelSend() {
    chatController?.abort();
  }

  // Picking a group channel is an intent to watch it — exit any DM overlay and
  // drop a stale publish error from the prior selection.
  function onChannelChange() {
    publishError = "";
    if (selectedAgent) {
      exitDM();
    }
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
  <h2>Channels</h2>
  <p class="identity">Acting as <code>{userId}</code></p>

  {#if channelsError}
    <p class="boot error" role="alert">{channelsError}</p>
    <button type="button" class="retry" onclick={loadChannels}>Retry</button>
  {:else if !channelsLoaded || !agentsLoaded}
    <p class="loading" role="status">Loading…</p>
  {:else if bothEmpty}
    <!-- Merged onboarding (§D): only a stack with neither personas nor channels
         is a dead end. One first-contact surface for both entry points. -->
    <OnboardingEmpty
      title="No personas or channels yet."
      onRetry={() => {
        loadAgents();
        loadChannels();
      }}
    >
      Register a persona with <code>persatrix agent register</code> to start a
      DM, or define group channels in <code>config/channels.yaml</code>, then
      re-check.
    </OnboardingEmpty>
  {:else}
    {#if agents.length > 0}
      <!-- DM entry point (§B): pick a persona to start/open a direct message.
           No lobby prompt over the picker — the channel timeline fills the body
           when no DM is open. -->
      <PersonaPicker
        bind:selectedAgent
        {agents}
        {sending}
        onChange={onPersonaPick}
        onExit={exitDM}
      />
    {/if}

    <ChannelPicker
      {groupChannels}
      bind:selectedChannel
      {canCreate}
      {onChannelChange}
      onRefresh={loadChannels}
      onNewChannel={() => (showCreateForm = true)}
    />

    {#if canCreate && showCreateForm}
      <CreateChannelForm {agents} {userId} onCreated={onChannelCreated} onCancel={() => (showCreateForm = false)} />
    {/if}

    {#if canCreate && selectedChannel && !isDM}
      <!-- Manage the watched group channel's roster (add/remove members, set
           dispositions). Same capability as create; hidden for DMs. -->
      <ChannelMembers channelId={selectedChannel} members={selectedChannelMembers} {agents} {agentsById} {userId} onChanged={loadChannels} />
    {/if}

    {#if canConfigEdit && selectedChannel && !isDM}
      <!-- Governance settings for the watched group channel (RFC 0050 P2);
           config-edit capability only (independent of create), hidden for DMs. -->
      <ChannelSettings channelId={selectedChannel} members={selectedChannelMembers} {agentsById} onChanged={loadChannels} />
    {/if}

    {#if isDM}
      <PersonaHeader info={selectedAgentInfo} />
      {#if dmResolving}
        <p class="loading" role="status">Opening conversation…</p>
      {/if}
      {#if dmResolveError}
        <p class="poll-error" role="status">{dmResolveError}</p>
      {/if}
    {/if}

    {#if !activeChannel && !isDM}
      <!-- Neutral default: name both ways in (only reachable when at least one
           entry point exists — bothEmpty is handled above). -->
      <p class="empty">
        Select a persona to direct-message, or a channel to watch.
      </p>
    {:else}
      <ConversationFeed bind:this={feed} channelId={activeChannel} {userId} {agentsById} {isDM} peerId={selectedAgent} members={selectedChannelMembers} onCancelTurn={isDM && sending ? cancelSend : null} />
    {/if}

    {#if isDM}
      {#if sendError}
        <p class="boot error" role="alert">{sendError}</p>
      {/if}
      <DmComposer
        bind:message
        bind:sessionId
        bind:epochId
        {sending}
        {canSend}
        chattable={selectedAgentChattable}
        hasPersona={Boolean(selectedAgentInfo)}
        onSubmit={onDmSubmit}
        onKeydown={onDmKeydown}
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
        onSubmit={onPublishSubmit}
        onKeydown={onPublishKeydown}
      />
    {/if}
  {/if}
</section>
