// The DM half of the conversation panel (RFC 0048 chat-panel-retirement
// amendment §B): opening a persona's direct message, resolving its `dm:`
// channel read-only, and the synchronous chat-façade turn. Split out of
// ChannelTimeline.svelte, which owns the group-channel half and the layout; the
// panel renders the DM through the same ConversationFeed as a group channel (a
// chat IS a `dm:` channel server-side) and reads this controller's state.
//
// A `.svelte.js` module so the fields are reactive runes the panel's markup and
// bindings (the composer's draft + scope) read directly.
import { getChatHistory, sendChat, ApiError } from "./api.js";
import { isChattable } from "./agents.js";
import { selection } from "./selection.svelte.js";

// Mirrors the server's rune limit (chat_handler.go) for immediate feedback;
// counts code points, not UTF-16 units. The server still enforces it.
export const MAX_MESSAGE_LENGTH = 4000;

export class DmSession {
  agent = $state(""); // the DM persona's id, "" = group mode
  channelId = $state(""); // the resolved DM channel, "" until the first send
  resolving = $state(false);
  resolveError = $state("");
  message = $state(""); // the draft (an abortable synchronous turn)
  sessionId = $state("");
  epochId = $state("");
  sending = $state(false);
  sendError = $state("");

  #token = 0;
  #controller = null;
  #ctx;
  // Unsent drafts per persona (this page load): leaving a DM parks its draft
  // and opening one brings back its own.
  #drafts = new Map();

  // ctx: { userId(): the effective identity; feed(): the ConversationFeed
  // handle (markThinking/clearThinking/pollNow) or null; persona(id): the agent
  // record for an id, or null }.
  constructor(ctx) {
    this.#ctx = ctx;
  }

  // resolve reads the persona's canonical DM channel id WITHOUT creating it (a
  // never-messaged persona returns 200-empty, not 404 — the read-only LookupDM
  // path, slice1-ux §B). The id drives the feed.
  resolve(agentId) {
    const token = ++this.#token;
    this.resolving = true;
    this.resolveError = "";
    return getChatHistory(agentId, { userId: this.#ctx.userId() })
      .then((result) => {
        if (token !== this.#token) return;
        const msgs = result.messages ?? [];
        this.channelId = msgs.length > 0 ? (msgs[0].channel_id ?? "") : "";
      })
      .catch((err) => {
        if (token !== this.#token) return;
        this.resolveError = `Could not open the conversation: ${err.message}`;
        this.channelId = "";
      })
      .finally(() => {
        if (token === this.#token) this.resolving = false;
      });
  }

  // open enters DM mode for a persona and resolves its channel; records the
  // sticky selection so a tab round-trip resumes it. The panel's group
  // selection is left intact — the context exit() returns to.
  open(agentId) {
    if (!agentId) return;
    this.#park();
    selection.dmAgent = agentId;
    this.agent = agentId;
    this.channelId = "";
    this.message = this.#drafts.get(agentId) ?? "";
    this.sendError = "";
    this.resolveError = "";
    return this.resolve(agentId);
  }

  // exit leaves the DM for the group view; the null sentinel records a
  // deliberate exit so a tab switch doesn't auto-reopen it.
  exit() {
    this.#park();
    selection.dmAgent = null;
    this.agent = "";
    this.channelId = "";
    this.resolveError = "";
    this.message = "";
    this.sendError = "";
  }

  // #park keeps the open DM's unsent draft for when it is opened again.
  #park() {
    if (this.agent) {
      this.#drafts.set(this.agent, this.message);
    }
  }

  // send issues one synchronous turn and refreshes from the persisted channel:
  // an existing DM polls its head (surfacing the stored user turn + reply); a
  // fresh DM re-resolves its now-created channel id, driving the feed to load +
  // poll. The persisted messages are the single source of truth — no local
  // echo, so the poll can never double-show the turn.
  async send() {
    if (this.sending) {
      return;
    }
    this.sendError = "";
    const text = this.message.trim();
    if (!this.agent || text.length === 0) {
      return;
    }
    // Enter-to-send bypasses the disabled button, so re-check chattability.
    if (!isChattable(this.#ctx.persona(this.agent))) {
      return;
    }
    if ([...text].length > MAX_MESSAGE_LENGTH) {
      this.sendError = `Message exceeds the maximum length of ${MAX_MESSAGE_LENGTH} characters.`;
      return;
    }

    const agentAtSend = this.agent;
    const hadChannel = Boolean(this.channelId);
    const feed = () => this.#ctx.feed();
    this.sending = true;
    this.#controller = new AbortController();
    // The synchronous turn IS the DM's "thinking" signal (cleared below/finally).
    feed()?.markThinking([agentAtSend]);
    try {
      const payload = {
        message: text,
        userId: this.#ctx.userId(),
        signal: this.#controller.signal,
      };
      const usedSession = this.sessionId.trim();
      const usedEpoch = this.epochId.trim();
      if (usedSession) payload.sessionId = usedSession;
      if (usedEpoch) payload.epochId = usedEpoch;
      await sendChat(agentAtSend, payload);
      // The draft parked for this persona, if the DM was left mid-turn, is
      // this very message — it has been sent.
      this.#drafts.delete(agentAtSend);
      // Switched persona mid-turn: the reply belongs to a DM already left.
      if (agentAtSend !== this.agent) {
        return;
      }
      this.message = "";
      feed()?.clearThinking([agentAtSend], { replied: true });
      if (hadChannel) {
        feed()?.pollNow();
      } else {
        this.resolve(agentAtSend);
      }
    } catch (err) {
      // A user cancel surfaces as an AbortError (status-0 ApiError wrapping it):
      // not a failure — drop it silently rather than alarming over an own cancel.
      if (this.#controller?.signal.aborted || err?.cause?.name === "AbortError") {
        // intentionally no sendError
      } else {
        this.sendError =
          err instanceof ApiError
            ? err.message
            : `The message could not be sent: ${err.message}`;
      }
    } finally {
      this.sending = false;
      this.#controller = null;
      // Backstop for cancel/error/mid-turn switch — clears with no idle flash.
      feed()?.clearThinking([agentAtSend]);
    }
  }

  cancel() {
    this.#controller?.abort();
  }
}
