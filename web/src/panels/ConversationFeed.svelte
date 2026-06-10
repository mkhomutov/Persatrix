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
  import { getChannelHistory, getChannelActivity } from "../lib/api.js";
  import { participantAgentIds } from "../lib/interactions.js";
  import { GRACE_MS } from "../lib/presence.js";
  import ChannelMessage from "./ChannelMessage.svelte";
  import InteractionSummary from "./InteractionSummary.svelte";
  import PresenceBar from "./PresenceBar.svelte";
  import { createPresence } from "../lib/presence.svelte.js";

  // channelId — the conversation to show ("" = a clean empty view, e.g. a fresh
  //   DM with no channel yet); userId/agentsById — sender decoration.
  // isDM/peerId/members — describe the active conversation's participants so the
  //   v0.3.8 interaction-summary surface can query their closed-interaction
  //   summaries (a DM's peer, or a group channel's members); the active channel
  //   id doubles as the RFC 0020 scope.
  // onCancelTurn — when set (a DM send in flight), the PresenceBar offers a
  //   Cancel for the synchronous round-trip.
  let {
    channelId,
    userId,
    agentsById = {},
    isDM = false,
    peerId = "",
    members = [],
    onCancelTurn = null,
  } = $props();

  // The live-presence controller (RFC 0048 console). Owned here — beside the
  // timeline it annotates, the poll that feeds its server signal, and the one
  // that clears it — and driven by the panel through markThinking()/
  // clearThinking(), the same handle pattern as echo()/pollNow(). A group also
  // polls the authoritative /activity set into it (Tier 1); a DM relies on the
  // optimistic overlay alone. See lib/presence.svelte.js.
  const presence = createPresence();
  $effect(() => () => presence.dispose());

  // The conversation this presence belongs to. A DM is keyed by its peer, NOT
  // its channel id: a fresh DM resolves its channel id only after the first send
  // (channelId "" → "dm:…"), and that same-turn fill-in must keep the optimistic
  // state (including the "Waiting for you" idle flash) it just set. A group is
  // keyed by its channel id directly. When the key changes the operator has
  // switched conversations, so reset — the optimistic signal only ever describes
  // turns triggered in the conversation it was triggered in, and must not bleed
  // (nor merge a stale pending set) into the next one.
  const conversationKey = $derived(isDM ? `dm:${peerId}` : channelId);
  $effect(() => {
    void conversationKey;
    presence.reset();
  });

  // markThinking/clearThinking are the owner's handles onto the indicator: the
  // panel lights a turn at send/publish and clears it on cancel/error (a reply
  // clears itself via the poll-tick pruneFrom + /activity read below). A group
  // add carries a grace window — the /activity poll confirms it within a tick,
  // and a wrong guess fades; a DM add is sticky (no /activity poll backs it).
  export function markThinking(ids) {
    presence.add(ids, isDM ? undefined : { graceMs: GRACE_MS });
  }
  export function clearThinking(ids, opts) {
    presence.remove(ids, opts);
  }

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
    // Issue the activity read alongside the head fetch — the two are
    // independent, and serializing them would stretch every tick by a full
    // round-trip. Only the INSTALL order matters: set() lands before pruneFrom
    // (awaited below), so the prune can still trim the just-installed server
    // set for an agent whose reply this tick surfaced. pollActivity never
    // rejects (self-guarded), so a head-fetch error can't leak an unhandled
    // rejection from the un-awaited branch.
    const activityRead = pollActivity(channel, token);
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
      // Reconcile the authoritative thinking set, THEN prune locally-seen
      // replies — pruneFrom bridges the gap before the next /activity read
      // drops a freshly-replied agent server-side.
      await activityRead;
      if (fresh.length > 0) {
        presence.pruneFrom(fresh);
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

  // pollActivity reads the orchestrator's authoritative in-flight set and feeds
  // it to the controller (Tier 1). Group channels only — a DM is single-trigger,
  // fully covered by the optimistic overlay, so it skips the extra request.
  // Self-guarded: an activity failure must NOT pause the message timeline or
  // back the whole poll off, so it swallows its own error (and the
  // unknown-in-test case where the mock omits getChannelActivity) — the
  // optimistic overlay holds and the next tick recovers.
  //
  // Two guards beyond the channel-switch token:
  //   • newest-read-wins — the on-open read (loadHistory) is fire-and-forget
  //     and races the poll ticks under the same token; activitySeq drops any
  //     resolution that is no longer the latest issued, so a slow straggler
  //     cannot overwrite a fresher set.
  //   • the operator's own id is filtered out — the server marks every
  //     candidate responder (orderResponders), and a human channel member is a
  //     candidate like any other (e.g. an agent @-mentions the console user),
  //     so the raw set can contain userId. Mirrors the optimistic path's
  //     `id !== userId` filter in ChannelTimeline; the bar must never tell the
  //     operator they are "thinking". Other ids pass through unfiltered —
  //     shortAgentName's raw-id fallback owns the unknown-agent case.
  let activitySeq = 0;
  async function pollActivity(channel, token) {
    if (isDM) return;
    const seq = ++activitySeq;
    try {
      const { thinking } = await getChannelActivity(channel);
      if (token === loadToken && seq === activitySeq) {
        presence.set((thinking ?? []).filter((id) => id !== userId));
      }
    } catch {
      // keep the last-known / optimistic set; the dead-poll backstop covers a
      // sustained outage.
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
        // Surface an already-in-flight round the moment a group opens (a reload
        // or a tab switch), rather than waiting for the first poll tick.
        pollActivity(channel, token);
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

<!-- Live status, directly above the composer (RFC 0048 presence): "… is
     thinking", softening to "taking a while", then a brief "Waiting for you"
     when the turn returns. A DM round-trip can be cancelled from here. -->
<PresenceBar
  thinking={presence.thinking}
  {agentsById}
  slow={presence.slow}
  idle={presence.idle}
  onCancel={onCancelTurn}
/>
