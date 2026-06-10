// The live-presence controller behind the conversation panel's PresenceBar
// (RFC 0048 console). A `.svelte.js` module so the `$state` runes it holds stay
// reactive across the component that renders them — ConversationFeed owns an
// instance and reads its getters. It fuses two sources into one displayed
// "thinking" set (see lib/presence.js mergeThinking):
//
//   • server — the orchestrator's authoritative per-channel /activity set, fed
//     in via set() on each group poll tick. Accurate for EVERY trigger (another
//     participant, an autonomous reply, a cascade) and across reloads.
//   • optimistic — a short overlay for the console's OWN just-fired turns,
//     added via add() so the indicator lights instantly rather than waiting up
//     to a poll interval for the server to confirm. A group add carries a grace
//     expiry: if the server never confirms it (a wrong guess), it fades. A DM
//     add is sticky (Infinity) — DMs don't poll /activity (single-trigger), so
//     the synchronous send lifecycle drives it via add()/remove() alone.
//
// The displayed set drives edge-triggered UX: the "taking a while…" softening on
// the leading edge, a "Waiting for you" flash when a turn hands back to the
// operator, and a dead-poll backstop that self-clears a frozen indicator.
import {
  mergeThinking,
  SLOW_AFTER_MS,
  GRACE_MS,
  STALE_AFTER_MS,
} from "./presence.js";

// How long the "Waiting for you" hint lingers after a turn returns to the
// operator — long enough to register the hand-back, short enough not to become
// permanent chrome.
const IDLE_FLASH_MS = 4000;

// createPresence builds one controller. Thresholds + clock are injectable so a
// test can drive the transitions deterministically.
export function createPresence({
  slowAfterMs = SLOW_AFTER_MS,
  idleFlashMs = IDLE_FLASH_MS,
  graceMs = GRACE_MS,
  staleAfterMs = STALE_AFTER_MS,
  now = () => Date.now(),
} = {}) {
  let thinking = $state([]); // the fused, displayed set
  let slow = $state(false); // soften the copy once a turn drags on
  let idle = $state(false); // the brief "Waiting for you" flash
  let serverIds = []; // last authoritative /activity set (plain)
  const grace = new Map(); // optimistic id -> grace-expiry ms (Infinity = sticky)
  let slowTimer = null;
  let idleTimer = null;
  let staleTimer = null;
  let graceTimer = null;

  function clearActiveTimers() {
    clearTimeout(slowTimer);
    clearTimeout(staleTimer);
    clearTimeout(graceTimer);
    slowTimer = null;
    staleTimer = null;
    graceTimer = null;
  }

  function flashIdle() {
    idle = true;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      idle = false;
    }, idleFlashMs);
  }

  // recompute folds the two sources into `thinking`, prunes lapsed optimistic
  // entries, and drives the timers + idle flash. flashOnEmpty distinguishes a
  // real hand-back (a reply emptied the set, or the server cleared a turn it
  // had confirmed) from a mere add — and from a wrong optimistic guess fading,
  // which is degradation and stays silent (see set()).
  function recompute({ flashOnEmpty = false } = {}) {
    const entries = Array.from(grace, ([id, expiresAt]) => ({ id, expiresAt }));
    const { ids, expired } = mergeThinking(serverIds, entries, now());
    expired.forEach((id) => grace.delete(id));

    // Self-arm the next grace fade. The fade must be timer-driven: the
    // /activity poll is self-guarded, so a persistent failure means set() never
    // runs, and without this timer a never-confirmed guess would outlive its
    // graceMs bound — staying lit (and softening at slowAfterMs) until the
    // stale backstop. Sticky (Infinity) entries never arm it, so a quiet DM
    // holds no recurring timer.
    clearTimeout(graceTimer);
    graceTimer = null;
    let nextFade = Infinity;
    for (const expiresAt of grace.values()) {
      if (expiresAt < nextFade) nextFade = expiresAt;
    }
    if (nextFade !== Infinity) {
      graceTimer = setTimeout(() => recompute(), Math.max(0, nextFade - now()));
    }

    const prev = new Set(thinking);
    const wasActive = prev.size > 0;
    thinking = ids;

    if (ids.length > 0) {
      // Re-arm the dead-poll backstop every recompute so a server-confirmed
      // turn persists. The slow countdown restarts whenever the set GAINS an
      // id: "taking a while…" describes the CURRENT turn, so a new turn
      // joining (an optimistic add, or the server rotating to the next floor
      // speaker without the set ever emptying) resets the softening rather
      // than inheriting the previous turn's elapsed countdown. A mere
      // reconfirmation of the same set (every poll tick) must NOT re-arm it,
      // or a long turn would never soften.
      clearTimeout(staleTimer);
      staleTimer = setTimeout(hardClear, staleAfterMs);
      if (ids.some((id) => !prev.has(id))) {
        slow = false;
        clearTimeout(slowTimer);
        slowTimer = setTimeout(() => {
          slow = true;
        }, slowAfterMs);
      }
    } else {
      clearActiveTimers();
      slow = false;
      if (wasActive && flashOnEmpty) {
        flashIdle();
      }
    }
  }

  // hardClear is the dead-poll backstop: /activity stopped answering (or a DM
  // reply never landed) long enough that the indicator would otherwise freeze.
  // No idle flash — this is degradation, not a real hand-back.
  function hardClear() {
    serverIds = [];
    grace.clear();
    clearActiveTimers();
    thinking = [];
    slow = false;
  }

  return {
    get thinking() {
      return thinking;
    },
    get slow() {
      return slow;
    },
    get idle() {
      return idle;
    },

    // set installs the authoritative server signal (one /activity read). The
    // "Waiting for you" hand-back flash is gated on the server having actually
    // confirmed a turn before this read: a display that empties while the
    // server set was empty throughout means a wrong optimistic guess fading —
    // no reply ever landed, so flashing would announce a hand-back that never
    // happened. Silent, like hardClear: degradation, not a turn returning.
    set(ids) {
      const hadServer = serverIds.length > 0;
      serverIds = (ids ?? []).filter(Boolean);
      recompute({ flashOnEmpty: hadServer });
    },

    // add lights the console's own turn instantly. graceMs bounds an unconfirmed
    // group add; omit it (DM) for a sticky overlay the send lifecycle clears.
    add(ids, { graceMs: g } = {}) {
      const expiresAt = g == null ? Infinity : now() + g;
      let any = false;
      for (const id of (ids ?? []).filter(Boolean)) {
        grace.set(id, expiresAt);
        any = true;
      }
      if (!any) return;
      idle = false;
      clearTimeout(idleTimer);
      recompute();
    },

    // remove clears the given ids from BOTH sources (a DM reply/cancel, or a
    // mid-turn switch). With `replied` it flashes the hand-back; without, it is a
    // silent backstop (cancel/error).
    remove(ids, { replied = false } = {}) {
      const drop = new Set(ids ?? []);
      drop.forEach((id) => grace.delete(id));
      serverIds = serverIds.filter((id) => !drop.has(id));
      recompute({ flashOnEmpty: replied });
    },

    // pruneFrom clears any pending persona whose reply just arrived on a poll
    // tick — the instant local clear that bridges the gap before the next
    // authoritative /activity read drops it server-side.
    pruneFrom(messages) {
      const senders = new Set(
        (messages ?? []).map((m) => m && m.sender_id).filter(Boolean),
      );
      if (senders.size === 0) return;
      let touched = false;
      senders.forEach((s) => {
        if (grace.delete(s)) touched = true;
      });
      const nextServer = serverIds.filter((id) => !senders.has(id));
      if (nextServer.length !== serverIds.length) {
        serverIds = nextServer;
        touched = true;
      }
      if (touched) recompute({ flashOnEmpty: true });
    },

    // reset drops all state + timers for a conversation switch: the signal only
    // describes the conversation it was gathered in, so nothing bleeds across.
    reset() {
      serverIds = [];
      grace.clear();
      clearActiveTimers();
      clearTimeout(idleTimer);
      idleTimer = null;
      thinking = [];
      slow = false;
      idle = false;
    },

    // dispose tears the timers down on unmount so a backgrounded tab can't fire
    // a stray state write after the panel is gone.
    dispose() {
      clearActiveTimers();
      clearTimeout(idleTimer);
    },
  };
}
