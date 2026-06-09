<script>
  // The shared conversation feed (RFC 0048 chat-panel-retirement amendment §B):
  // history load + live polling + the rendered timeline, for ONE channel id —
  // used identically by a group channel and a DM (a chat IS a `dm:` channel). The
  // panel owns the pickers, mode, and composers and just hands this the active
  // channel id; the write paths (publish echo / post-send refresh) call the two
  // exported handles. Extracted from ChannelTimeline.svelte to keep both under the
  // review-size cap.
  //
  // Polling protects the unauthenticated localhost surface: visibility-pause,
  // exponential error-backoff, and a head-poll that de-dupes by id (never a full
  // re-fetch per tick). A loadToken invalidates in-flight work when the channel
  // switches, so a stale resolution can't write to the wrong conversation.
  import { getChannelHistory } from "../lib/api.js";
  import { participantAgentIds } from "../lib/interactions.js";
  import ChannelMessage from "./ChannelMessage.svelte";
  import InteractionSummary from "./InteractionSummary.svelte";

  // channelId — the conversation to show ("" = a clean empty view, e.g. a fresh
  //   DM with no channel yet); userId/agentsById — sender decoration.
  // isDM/peerId/members — describe the active conversation's participants so the
  //   v0.3.8 interaction-summary surface can query their closed-interaction
  //   summaries (a DM's peer, or a group channel's members); the active channel
  //   id doubles as the RFC 0020 scope.
  let {
    channelId,
    userId,
    agentsById = {},
    isDM = false,
    peerId = "",
    members = [],
  } = $props();

  // The personas whose closed-interaction summaries the surface queries for this
  // scope, with the human principal excluded. Empty → no affordance.
  const summaryAgentIds = $derived(
    participantAgentIds({ isDM, peerId, members, exclude: userId }),
  );

  const POLL_INTERVAL_MS = 3000;
  const MAX_BACKOFF_MS = 30000;
  const HISTORY_LIMIT = 50;
  const HEAD_LIMIT = 50;

  // messages is newest-first (wire order); the panel renders oldest-top.
  let messages = $state([]);
  let historyError = $state("");
  let historyLoaded = $state(false);
  let pollError = $state("");

  let seenIds = new Set();
  let pollTimer = null;
  let backoffMs = POLL_INTERVAL_MS;
  let polling = false;
  let loadToken = 0;

  function clearPoll() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  // scheduleNext arms the next poll unless the tab is backgrounded (parked until
  // the visibility handler resumes). Clears any pending timer so two can't stack.
  function scheduleNext(delay) {
    clearPoll();
    if (typeof document !== "undefined" && document.hidden) {
      return;
    }
    pollTimer = setTimeout(poll, delay);
  }

  // poll fetches the channel head, appends only unseen messages, and reschedules
  // at the base interval (success) or a backed-off delay (error). The token guard
  // drops a result whose channel was switched out mid-flight; the in-flight guard
  // stops the visibility handler from launching a second concurrent fetch.
  async function poll() {
    clearPoll();
    const channel = channelId;
    const token = loadToken;
    if (!channel || polling) {
      return;
    }
    polling = true;
    try {
      const { messages: head } = await getChannelHistory(channel, {
        limit: HEAD_LIMIT,
      });
      if (token !== loadToken) {
        return;
      }
      const fresh = head.filter((m) => !seenIds.has(m.id));
      if (fresh.length > 0) {
        fresh.forEach((m) => seenIds.add(m.id));
        messages = [...fresh, ...messages];
      }
      pollError = "";
      backoffMs = POLL_INTERVAL_MS;
      scheduleNext(POLL_INTERVAL_MS);
    } catch (err) {
      if (token !== loadToken) {
        return;
      }
      pollError = `Live updates paused: ${err.message}`;
      backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
      scheduleNext(backoffMs);
    } finally {
      polling = false;
    }
  }

  // loadHistory replaces the timeline with the channel's history and (re)starts
  // polling from a clean de-dupe set; the loadToken bump invalidates prior work.
  function loadHistory(channel) {
    const token = ++loadToken;
    clearPoll();
    historyError = "";
    pollError = "";
    historyLoaded = false;
    messages = [];
    seenIds = new Set();
    backoffMs = POLL_INTERVAL_MS;
    polling = false;
    return getChannelHistory(channel, { limit: HISTORY_LIMIT })
      .then(({ messages: history }) => {
        if (token !== loadToken) return;
        messages = history;
        history.forEach((m) => seenIds.add(m.id));
        scheduleNext(POLL_INTERVAL_MS);
      })
      .catch((err) => {
        if (token !== loadToken) return;
        historyError = `Could not load history: ${err.message}`;
      })
      .finally(() => {
        if (token !== loadToken) return;
        historyLoaded = true;
      });
  }

  // retryHistory re-runs the load after an initial failure (the poll loop only
  // arms on success, and re-selecting the same channel fires no change event).
  function retryHistory() {
    if (channelId) {
      loadHistory(channelId);
    }
  }

  // React to the active channel: load + poll. A "" id is a settled empty view (a
  // fresh DM, or no selection), not a perpetual loader.
  $effect(() => {
    const channel = channelId;
    if (!channel) {
      clearPoll();
      messages = [];
      seenIds = new Set();
      historyError = "";
      pollError = "";
      historyLoaded = true;
      return;
    }
    loadHistory(channel);
    return () => {
      loadToken++;
      clearPoll();
    };
  });

  // Pause polling while backgrounded; catch up on reveal.
  $effect(() => {
    function onVisibility() {
      if (document.hidden) {
        clearPoll();
      } else if (channelId && historyLoaded && !historyError) {
        poll();
      }
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  });

  // pollNow surfaces a just-persisted turn (a DM send) on an immediate head poll,
  // rather than waiting for the next timer tick.
  export function pollNow() {
    poll();
  }

  // echo appends a just-published message immediately and seeds its id so the next
  // poll won't re-add it (the group-mode publish path).
  export function echo(stored) {
    if (stored && stored.id && !seenIds.has(stored.id)) {
      seenIds.add(stored.id);
      messages = [stored, ...messages];
    }
  }

  // Render oldest-top, newest-bottom; reverse a shallow copy (the model + de-dupe
  // stay newest-first).
  const displayMessages = $derived(messages.slice().reverse());

  // Pinned-scroll autoscroll: a new message scrolls to the bottom ONLY when the
  // operator is already there, so reading history isn't yanked away.
  let timelineEl = $state(null);
  let pinnedToBottom = true;
  const PIN_EPSILON_PX = 40;

  function onTimelineScroll() {
    if (!timelineEl) return;
    const distance =
      timelineEl.scrollHeight - timelineEl.scrollTop - timelineEl.clientHeight;
    pinnedToBottom = distance < PIN_EPSILON_PX;
  }

  $effect(() => {
    void displayMessages.length;
    if (timelineEl && pinnedToBottom) {
      timelineEl.scrollTop = timelineEl.scrollHeight;
    }
  });
</script>

{#if pollError}
  <!-- Non-fatal: the loaded history stays visible while the poll backs off. -->
  <p class="poll-error" role="status">{pollError}</p>
{/if}

{#if historyError}
  <p class="boot error" role="alert">{historyError}</p>
  <button type="button" class="retry" onclick={retryHistory}>Retry</button>
{:else if !historyLoaded}
  <p class="loading" role="status">Loading messages…</p>
{:else if messages.length === 0}
  <p class="empty">No messages yet.</p>
{:else}
  <ol
    class="timeline"
    aria-label="Channel messages"
    bind:this={timelineEl}
    onscroll={onTimelineScroll}
  >
    {#each displayMessages as message (message.id)}
      <ChannelMessage {message} {userId} {agentsById} />
    {/each}
  </ol>
{/if}

<!-- Interaction-summary surface (v0.3.8): the synthesised outcome of a closed
     interaction, below the live turns. Self-fetching + additive — renders
     nothing while the conversation is open. -->
{#if channelId}
  <InteractionSummary
    scope={channelId}
    agentIds={summaryAgentIds}
    {agentsById}
    {userId}
  />
{/if}
