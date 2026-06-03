<script>
  // Chat panel (RFC 0048 Phase 1 PR 4) — the "feel the taste" moment: pick a
  // persona and talk to it over today's synchronous chat API. No backend change;
  // pure render-over-existing-API. The shell threads in the /ui/context-derived
  // userId (RFC §F single identity source), so the panel never prompts for or
  // hard-codes a user.
  import { listAgents, sendChat, getChatHistory, ApiError } from "../lib/api.js";
  import ScopeSelector from "./ScopeSelector.svelte";
  import OnboardingEmpty from "./OnboardingEmpty.svelte";
  import PersonaHeader from "./PersonaHeader.svelte";
  import ChatMessage from "./ChatMessage.svelte";
  import { nav } from "../lib/nav.svelte.js";
  import { selection, pickInitialAgent } from "../lib/selection.svelte.js";

  let { userId } = $props();

  // chatMaxMessageLength mirrors the server's constant (chat_handler.go) so an
  // over-length message is rejected with immediate feedback; the server still
  // enforces it — this is a courtesy guard. It measures in runes
  // (utf8.RuneCountInString), so the guard counts code points too
  // (`[...text].length`) rather than over-counting astral chars via UTF-16.
  const MAX_MESSAGE_LENGTH = 4000;

  let agents = $state([]);
  let agentsError = $state("");
  // agentsLoaded flips true once the first (or a retried) load settles, so the
  // "no personas" empty state only shows after a confirmed-empty list — never as
  // a flash of the blank picker while the load is still in flight.
  let agentsLoaded = $state(false);
  let selectedAgent = $state("");
  let message = $state("");
  let sessionId = $state("");
  let epochId = $state("");
  let sending = $state(false);
  let sendError = $state("");
  // chatController backs the abortable turn (§D): a synchronous chat can block up
  // to the 30s server timeout, so it is held across the await for Cancel to abort.
  let chatController = null;
  // The transcript is a flat, conversational (oldest-top) message list (RFC 0048
  // amendment §B). Each entry is one message:
  //   { id, fromUser, who, content, status?, timestamp, session?, epoch? }
  // On persona-select it is SEEDED from the persisted DM history (so a reload
  // resumes the conversation), then live turns append to the bottom.
  let transcript = $state([]);
  // Live (this-session) messages get a locally-minted id; seeded history uses
  // the server message id. The `local-` prefix can never collide with a server
  // id, keeping the {#each} key stable across a seed-then-send sequence.
  let nextLocalId = 0;
  // History-seed state, separate from the agent-list load: a persona switch
  // reloads THAT persona's conversation. historyError is non-fatal (you can
  // still send into a fresh transcript); historyToken guards superseded loads.
  let historyLoading = $state(false);
  let historyError = $state("");
  let historyToken = 0;
  // The resolved DM channel id, for the §F "view in timeline" deep-link. Empty
  // until a conversation exists; seeded from history on reseed (loadHistory) and
  // re-resolved after the first live send (see send()).
  let dmChannelId = $state("");

  const canSend = $derived(
    Boolean(selectedAgent) && message.trim().length > 0 && !sending,
  );

  // selectedAgentInfo is the full persona record behind the picker selection, so
  // the panel can render a persona header (name — role, capabilities) above the
  // transcript rather than leaving the persona a bare dropdown entry. The DTO
  // already carries role (RFC 0048 amendment §A) and capabilities/address.
  const selectedAgentInfo = $derived(
    agents.find((agent) => agent.id === selectedAgent) ?? null,
  );

  // Task agents (agents.yaml `type: "task"`) run workflow steps and never hold a
  // conversation, so a chat turn dead-ends in a timeout. They show in the picker
  // but disabled (extends the §A agent DTO); any non-"task" type — incl. an unset
  // one from an agent predating the field — stays chattable, so the guard can
  // never regress a real conversation.
  function isChattable(agent) {
    return agent?.type !== "task";
  }
  // selectedAgentChattable gates the composer: false only when a task agent is
  // selected (reachable just when the deployment has no persona to fall back to).
  const selectedAgentChattable = $derived(isChattable(selectedAgentInfo));

  // personaName is the label shown for the agent's own messages — its name (or
  // id), mirroring agentLabel's fallback.
  const personaName = $derived(
    selectedAgentInfo
      ? selectedAgentInfo.name || selectedAgentInfo.id
      : selectedAgent,
  );

  // historyToEntry maps a persisted DM message into a flat transcript entry. The
  // operator's own messages (sender_id === userId) render as "You"; everything
  // else in a DM is the persona. Seeded history carries no per-turn scope
  // annotation (a persisted message does not record the session/epoch override),
  // so by amendment §B caveat 2 the scope line is shown only on live turns.
  function historyToEntry(m) {
    const fromUser = m.sender_id === userId;
    return {
      id: m.id,
      fromUser,
      who: fromUser ? "You" : personaName,
      content: m.content,
      status: m.metadata?.reply_status,
      timestamp: m.timestamp,
    };
  }

  // loadHistory seeds the transcript from the selected persona's persisted DM,
  // making a reload resume the conversation (§B). The wire order is newest-first;
  // the panel renders oldest-top, so the seed is reversed. A fresh persona returns
  // an empty list (200, not 404) — a clean empty transcript, not an error.
  // historyError is non-fatal: a failed seed still leaves a usable composer.
  //
  // Scope note: the seed fetches only the server's default-limit page (50) and the
  // panel has no "load earlier" yet, so a longer conversation resumes from its
  // tail; getChatHistory already plumbs limit/before for the §F back-fill.
  function loadHistory(agentID) {
    const token = ++historyToken;
    historyError = "";
    historyLoading = true;
    transcript = [];
    dmChannelId = "";
    return getChatHistory(agentID, { userId })
      .then((result) => {
        if (token !== historyToken) return;
        const messages = result.messages ?? [];
        transcript = messages
          .slice()
          .reverse()
          .map(historyToEntry);
        // The history messages carry the canonical DM channel id; capture it for
        // the §F timeline deep-link. Empty history leaves it unset (no DM yet).
        if (messages.length > 0) {
          dmChannelId = messages[0].channel_id ?? "";
        }
      })
      .catch((err) => {
        if (token !== historyToken) return;
        historyError = `Could not load conversation history: ${err.message}`;
      })
      .finally(() => {
        if (token !== historyToken) return;
        historyLoading = false;
      });
  }

  // Reseed the transcript whenever the selected persona changes (including the
  // initial default selection): each persona is its own conversation, so the
  // transcript must follow the picker. The cleanup invalidates an in-flight seed
  // so a slow load can't write into a persona the operator already switched off.
  $effect(() => {
    const agent = selectedAgent;
    // Track userId too: the §E "acting as" override changes the effective
    // identity, and persistence is keyed on (user, agent) — so switching the
    // acting-as user must reseed the transcript with THAT user's conversation
    // (the whole point of the override — different user, different/empty
    // history). Reading it here makes the effect re-run on an identity change.
    void userId;
    if (!agent) return;
    loadHistory(agent);
    return () => {
      historyToken++;
    };
  });

  // onMessageKeydown wires the universal chat idiom (§D): Enter sends,
  // Shift+Enter inserts a newline. Without this the <textarea> swallows Enter and
  // the button is the only send path. IME composition (event.isComposing) is left
  // alone so Enter can commit a candidate without firing a send.
  function onMessageKeydown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  }

  // cancelSend aborts the in-flight turn (§D). The fetch rejects with an
  // AbortError, which send() recognises and treats as a quiet cancellation
  // rather than an error.
  function cancelSend() {
    chatController?.abort();
  }

  // viewInTimeline hands the current conversation to the Channel Timeline (§F):
  // it records the resolved DM channel id as the pending selection and switches
  // the hash route, so the freshly-mounted timeline opens on this conversation.
  // Only reachable once a conversation exists (dmChannelId is set from history).
  //
  // The affordance is a real <a href="#/channels">, so we preventDefault and drive
  // the route in JS: that records the nav intent before the route changes rather
  // than racing the anchor's native navigation.
  function viewInTimeline(event) {
    event?.preventDefault();
    if (!dmChannelId) return;
    nav.targetChannel = dmChannelId;
    window.location.hash = "#/channels";
  }

  // loadToken disambiguates concurrent/superseded loads: each call stamps a
  // token and only the latest may write state. This both guards against a
  // resolve-after-unmount (the effect cleanup bumps the token) and makes Retry
  // safe — a slow first load can never clobber the result of a later retry.
  let loadToken = 0;

  // loadAgents fetches the persona list and is re-runnable: the mount effect
  // calls it once, and the Retry control calls it again after a load failure.
  function loadAgents() {
    const token = ++loadToken;
    agentsError = "";
    agentsLoaded = false;
    return listAgents()
      .then((list) => {
        if (token !== loadToken) return;
        agents = list;
        // Resume the operator's last deliberately-chosen persona across a tab
        // switch (selection.svelte.js outlives the unmount that destroys the local
        // selection), else apply the healthy-first default — see pickInitialAgent.
        const initial = pickInitialAgent(list, isChattable, selection.chatAgent);
        if (initial) {
          selectedAgent = initial.id;
        }
      })
      .catch((err) => {
        if (token !== loadToken) return;
        agentsError = `Could not load personas: ${err.message}`;
      })
      .finally(() => {
        if (token !== loadToken) return;
        agentsLoaded = true;
      });
  }

  $effect(() => {
    loadAgents();
    return () => {
      // Invalidate any in-flight load so its resolution can't write to an
      // unmounted component.
      loadToken++;
    };
  });

  // agentLabel is the picker's display text: the persona's name, falling back to
  // its id when unnamed (matching the server's own display-name fallback in
  // chat_handler.go). A non-healthy persona is annotated with its status, since
  // only a healthy one can actually reply (the chat route 503s otherwise) — the
  // operator sees that before spending a send, not after.
  function agentLabel(agent) {
    const name = agent.name ? agent.name : agent.id;
    // Fold the role into the option so the picker reads as a cast of personas
    // ("Ada — Researcher") rather than a list of bare names (RFC 0048 §A). Role
    // is optional; omit the separator when unset.
    const named = agent.role ? `${name} — ${agent.role}` : name;
    // A task agent's row carries the why ("show but explain") rather than its
    // health — a disabled row can't be sent regardless of status, so the reason
    // it's disabled is the useful annotation.
    if (!isChattable(agent)) {
      return `${named} (task agent — not chattable)`;
    }
    return agent.status && agent.status !== "healthy"
      ? `${named} (${agent.status})`
      : named;
  }

  async function send() {
    // Guard re-entrancy: the Send button is disabled while a reply is in flight,
    // but pressing Enter in a single-line override input still submits the form
    // (canSend gates only the button, not send()). A second concurrent turn
    // collides on the server's replyWaiter (409) and wastes a round-trip, so
    // drop it here.
    if (sending) {
      return;
    }
    sendError = "";
    const text = message.trim();
    if (!selectedAgent || text.length === 0) {
      return;
    }
    // Enter-to-send bypasses the disabled button (canSend gates the button, not
    // send()), so re-check chattability here: a task agent can never be sent to.
    if (!isChattable(selectedAgentInfo)) {
      return;
    }
    if ([...text].length > MAX_MESSAGE_LENGTH) {
      sendError = `Message exceeds the maximum length of ${MAX_MESSAGE_LENGTH} characters.`;
      return;
    }

    sending = true;
    chatController = new AbortController();
    try {
      const payload = { message: text, userId, signal: chatController.signal };
      // Pass the optional isolation overrides through only when set, so an empty
      // selector leaves the orchestrator's boot defaults intact (the client
      // omits absent keys; see api.js sendChat).
      const usedSession = sessionId.trim();
      const usedEpoch = epochId.trim();
      if (usedSession) {
        payload.sessionId = usedSession;
      }
      if (usedEpoch) {
        payload.epochId = usedEpoch;
      }
      const response = await sendChat(selectedAgent, payload);
      // A live turn appends two flat messages — the operator's prompt then the
      // persona's reply — keeping the conversational oldest-top order. The user
      // message carries the scope it was actually sent under (the overrides stay
      // editable between turns, so pinning the scope per message keeps the
      // isolation story visible — RFC 0031 / ISSUE-0085). The reply timestamp
      // comes from the server when present, else the local clock.
      const now = new Date().toISOString();
      transcript = [
        ...transcript,
        {
          id: `local-${nextLocalId++}`,
          fromUser: true,
          who: "You",
          content: text,
          timestamp: now,
          session: usedSession,
          epoch: usedEpoch,
        },
        {
          id: `local-${nextLocalId++}`,
          fromUser: false,
          who: response.agent_display_name || personaName,
          content: response.reply,
          status: response.reply_status,
          timestamp: response.timestamp
            ? new Date(response.timestamp * 1000).toISOString()
            : now,
        },
      ];
      message = "";
      // First message of a fresh conversation creates the DM server-side (§F);
      // chatResponse omits its id, so re-resolve it from history (DM ids are never
      // hand-built — see channels.CanonicalDMID) to light up the hand-off this
      // turn. Fire-and-forget + token-guarded, and guarded to the first turn
      // (dmChannelId empty) so steady-state chatting adds no extra fetch.
      if (!dmChannelId && selectedAgent) {
        const token = historyToken;
        getChatHistory(selectedAgent, { userId })
          .then((r) => {
            const m = r.messages ?? [];
            if (token === historyToken && m.length > 0)
              dmChannelId = m[0].channel_id ?? "";
          })
          .catch(() => {});
      }
    } catch (err) {
      // A user-initiated cancel surfaces as an AbortError (fetch rejecting on
      // the aborted signal, wrapped as a status-0 ApiError with the AbortError
      // as its cause). That is not a failure — drop it silently rather than
      // alarming the operator with an error over their own deliberate cancel.
      if (chatController?.signal.aborted || err?.cause?.name === "AbortError") {
        // intentionally no sendError
      } else {
        // The client surfaces the server's `{error, code}` envelope as the
        // ApiError message (api.js), so showing err.message gives the operator
        // the backend's own wording (e.g. the over-length rejection). A
        // non-ApiError still degrades to its message rather than crashing.
        sendError =
          err instanceof ApiError
            ? err.message
            : `The message could not be sent: ${err.message}`;
      }
    } finally {
      sending = false;
      chatController = null;
    }
  }

  function onSubmit(event) {
    event.preventDefault();
    send();
  }

  // onPersonaChange clears a lingering send error when the operator switches
  // persona: the error refers to the attempt that just failed, and a new
  // selection is a fresh intent, so leaving the stale alert over the new picker
  // value reads as if the new persona is already broken. The transcript and the
  // in-progress message are left intact — only the transient error is dropped.
  // Scoped to the user-driven change event so the programmatic default-selection
  // during load doesn't clear an unrelated error. Recording the choice as the
  // sticky cross-mount selection here (not on the programmatic default) is what
  // lets a Channels round-trip resume THIS persona while a never-chosen panel
  // still re-evaluates the healthy-first default on each mount (pickInitialAgent).
  function onPersonaChange() {
    sendError = "";
    selection.chatAgent = selectedAgent;
  }

  // exitChat leaves the conversation entirely, returning to the persona lobby (no
  // persona selected) so the operator can start fresh or pick a different persona
  // — the web analogue of quitting the CLI chat REPL. The null sentinel records a
  // deliberate exit so it survives the unmount a tab switch causes (the panel
  // would otherwise re-apply its healthy-first default on remount — see
  // pickInitialAgent); clearing selectedAgent unmounts the composer/transcript.
  function exitChat() {
    selection.chatAgent = null;
    selectedAgent = "";
    historyToken++;
    transcript = [];
    dmChannelId = "";
    message = "";
    sendError = "";
    historyError = "";
  }
</script>

<section class="panel chat" aria-label="Chat">
  <h2>Chat</h2>
  <p class="identity">Acting as <code>{userId}</code></p>

  {#if agentsError}
    <p class="boot error" role="alert">{agentsError}</p>
    <button type="button" class="retry" onclick={loadAgents}>Retry</button>
  {:else if !agentsLoaded}
    <!-- Until the first load settles, show a loading line rather than falling
         through to the composer beside an empty picker (a flash of a blank
         dropdown). agentsLoaded gates both this and the empty state below. -->
    <p class="loading" role="status">Loading personas…</p>
  {:else if agents.length === 0}
    <!-- Onboarding, not a dead end (§F): a fresh stack has no personas yet, so
         say how to add one and offer a way to re-check without a full reload. -->
    <OnboardingEmpty title="No personas are registered yet." onRetry={loadAgents}>
      Register one with <code>persatrix agent register</code> (or add it to
      <code>config/agents.yaml</code> and restart), then re-check.
    </OnboardingEmpty>
  {:else}
    <!-- Persona switcher pinned at the top of the panel (mirrors the channel
         timeline's selector-at-top layout). It used to live at the bottom inside
         the composer, below the transcript — once a conversation grew, the only
         way to switch persona scrolled off-screen. Kept above the transcript and
         outside the composer so it stays reachable however long the chat runs.
         Still `disabled={sending}`: switching persona out from under an in-flight
         synchronous turn would misattribute the pending reply. -->
    <div class="persona-picker">
      <label>
        Persona
        <select
          bind:value={selectedAgent}
          onchange={onPersonaChange}
          disabled={sending}
        >
          {#if !selectedAgent}
            <!-- Lobby placeholder: no conversation is open (a fresh start or
                 after Exit). Disabled so it isn't a re-pickable value; it just
                 labels the empty selection until the operator chooses a persona. -->
            <option value="" disabled>Select a persona…</option>
          {/if}
          {#each agents as agent (agent.id)}
            <option value={agent.id} disabled={!isChattable(agent)}
              >{agentLabel(agent)}</option
            >
          {/each}
        </select>
      </label>
      {#if selectedAgent}
        <!-- Exit leaves the conversation for the persona lobby — the web analogue
             of quitting the CLI chat REPL. Locked during a turn so it can't
             strand an in-flight reply. -->
        <button
          type="button"
          class="exit-chat"
          onclick={exitChat}
          disabled={sending}>Exit</button
        >
      {/if}
    </div>

    {#if !selectedAgent}
      <!-- Lobby: no persona selected (a fresh start, or after Exit). Prompt the
           operator to pick one; the picker above is the entry point. No header /
           transcript / composer until a conversation is open. -->
      <p class="lobby" role="status">
        Select a persona to start a conversation.
      </p>
    {:else}
      <PersonaHeader
        info={selectedAgentInfo}
        {dmChannelId}
        onViewInTimeline={viewInTimeline}
      />

    {#if historyLoading}
      <p class="loading" role="status">Loading conversation history…</p>
    {/if}
    {#if historyError}
      <!-- Non-fatal: a failed history seed still leaves a usable (empty)
           transcript, so the operator can start a fresh conversation rather than
           being blocked. Muted, not an alarming alert. -->
      <p class="poll-error" role="status">{historyError}</p>
    {/if}

    <ol class="transcript" aria-label="Conversation">
      {#each transcript as msg (msg.id)}
        <ChatMessage {msg} />
      {/each}
    </ol>

    {#if sending}
      <p class="thinking" role="status">
        Waiting for a reply…
        <button type="button" class="cancel" onclick={cancelSend}>Cancel</button>
      </p>
    {/if}

    {#if sendError}
      <p class="boot error" role="alert">{sendError}</p>
    {/if}

    <form class="composer" onsubmit={onSubmit}>
      <!-- The composer locks while a turn is in flight (`sending`): the chat call
           is synchronous, so leaving it editable lets the operator keep typing
           into a message that the post-send reset then wipes. Disabling for the
           round-trip keeps the in-flight text; the "Waiting for a reply…" status
           says why. The persona switcher (lifted to the top of the panel) is
           disabled in lockstep so the turn stays pinned to its persona. -->
      <label>
        Message
        <textarea
          bind:value={message}
          rows="3"
          placeholder="Say something to the persona… (Enter to send, Shift+Enter for a new line)"
          disabled={sending}
          onkeydown={onMessageKeydown}
        ></textarea>
      </label>

      <!-- Optional isolation overrides (RFC 0031 session / ISSUE-0085 epoch),
           extracted to ScopeSelector. The §F identity rule constrains user_id
           only (never typed); session and epoch are operator-namespace ids the
           composer reads back via the bindings. -->
      <ScopeSelector bind:sessionId bind:epochId {sending} />

      {#if selectedAgentInfo && !selectedAgentChattable}
        <!-- Only reachable when the deployment has no persona to fall back to:
             explain why the composer is locked rather than leaving a dead Send. -->
        <p class="poll-error" role="status">
          Task agents run workflow steps and don't hold conversations — pick a
          persona to chat.
        </p>
      {/if}

      <button type="submit" disabled={!canSend || !selectedAgentChattable}>
        {sending ? "Sending…" : "Send"}
      </button>
    </form>
    {/if}
  {/if}
</section>
