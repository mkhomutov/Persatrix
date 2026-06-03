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

  let { userId } = $props();

  // chatMaxMessageLength mirrors the server's constant (chat_handler.go) so an
  // over-length message is rejected with immediate feedback instead of burning a
  // round-trip; the server still enforces it — this is a courtesy guard, not the
  // authority. The server measures in runes (utf8.RuneCountInString), so the
  // guard counts code points too (`[...text].length`) — a UTF-16 `.length` would
  // over-count astral characters and falsely block a message the server accepts.
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
  // chatController backs the abortable turn (§D): a synchronous chat can block
  // up to the 30s server timeout, so the in-flight turn is cancellable. Held
  // across the await so the Cancel control can abort the live fetch.
  let chatController = null;
  // The transcript is a flat, conversational (oldest-top) message list — the
  // shape RFC 0048 amendment §B migrates to. Each entry is one message:
  //   { id, fromUser, who, content, status?, timestamp, session?, epoch? }
  // On persona-select it is SEEDED from the persisted DM history (so a reload
  // resumes the conversation rather than presenting as stateless), then live
  // turns append to the bottom. A flat list (vs {prompt,reply} pairs) handles
  // the persisted ordering and any non-paired messages naturally.
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
  // already carries role (RFC 0048 amendment §A) and capabilities/address
  // (always served) — "faceless" was the client throwing those away, not the API
  // withholding them.
  const selectedAgentInfo = $derived(
    agents.find((agent) => agent.id === selectedAgent) ?? null,
  );

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
  // making a reload resume the conversation (the point of §B). The wire order is
  // newest-first; the panel renders oldest-top (conversational), so the seed is
  // reversed. A fresh persona returns an empty list (200, not 404) — a clean
  // empty transcript, not an error. historyError is non-fatal: a failed seed
  // still leaves a usable (empty) composer rather than blocking the panel.
  //
  // Scope note: the seed fetches only the server's default-limit most-recent
  // page (channelDefaultHistoryLimit, 50) and the panel has no "load earlier"
  // affordance yet, so a conversation longer than that resumes from its tail
  // with the oldest turns omitted. getChatHistory already plumbs limit/before
  // for the paginating back-fill a later slice (§F) adds; until then the cap is
  // intentional, not full resume.
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
  // the hash route, so the freshly-mounted timeline opens on this conversation —
  // making "your chat is a real, watchable channel" a click, not an assertion.
  // Only reachable once a conversation exists (dmChannelId is set from history).
  //
  // The affordance is a real <a href="#/channels"> (for link semantics), so we
  // preventDefault and drive the route in JS: that guarantees the nav intent is
  // recorded before the route changes, rather than racing the anchor's native
  // navigation — and a modified click (new tab) would land on a fresh context
  // without the intent anyway, so hijacking it to the in-place hand-off is the
  // behaviour we want.
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
        // Default the picker to the first *healthy* persona so a newcomer can
        // send immediately and the default landing isn't a guaranteed 503 (the
        // chat route only answers for a healthy persona; an offline one is a dead
        // end). Fall back to the first entry when none are healthy — there's
        // nothing more sendable to offer, and the status annotation already warns
        // why a send may fail.
        if (list.length > 0) {
          const healthy = list.find((agent) => agent.status === "healthy");
          selectedAgent = (healthy ?? list[0]).id;
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
      // chatResponse omits its id, so re-resolve it from history (the source of
      // truth — DM ids are never hand-built, see channels.CanonicalDMID) to light
      // up the hand-off this turn. Fire-and-forget + token-guarded: it neither
      // blocks the composer nor outlives a persona switch, and a failed capture
      // just leaves the link hidden until the next reseed. Guarded to the first
      // turn (dmChannelId empty) so steady-state chatting adds no extra fetch.
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
  // during load doesn't clear an unrelated error.
  function onPersonaChange() {
    sendError = "";
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
      <!-- The whole composer locks while a turn is in flight (`sending`): the
           chat call is synchronous, so leaving it editable lets the operator
           keep typing into a message that the post-send reset then wipes, or
           switch persona out from under the pending reply. Disabling for the
           round-trip keeps the in-flight text and pins the turn to its persona;
           the "Waiting for a reply…" status says why. -->
      <label>
        Persona
        <select
          bind:value={selectedAgent}
          onchange={onPersonaChange}
          disabled={sending}
        >
          {#each agents as agent (agent.id)}
            <option value={agent.id}>{agentLabel(agent)}</option>
          {/each}
        </select>
      </label>

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

      <button type="submit" disabled={!canSend}>
        {sending ? "Sending…" : "Send"}
      </button>
    </form>
  {/if}
</section>
