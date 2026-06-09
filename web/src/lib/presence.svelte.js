// The live-presence controller behind the conversation panel's PresenceBar
// (RFC 0048 console, Tier 0). A `.svelte.js` module so the `$state` runes it
// holds stay reactive across the component that renders them — the panel owns an
// instance and reads its getters, keeping the optimistic state machine out of
// ChannelTimeline (which is already at the review-size cap). The phrasing,
// pruning, and escalation thresholds it composes are the pure, unit-tested
// functions in lib/presence.js; this adds only the timers and the transitions.
//
// Optimism, and its limits, are documented in lib/presence.js: the controller
// only knows about turns this console triggered. Tier 1 (a server /activity
// signal) will feed `set()` directly instead.
import { pruneThinking, SLOW_AFTER_MS, EXPIRE_AFTER_MS } from "./presence.js";

// How long the "Waiting for you" hint lingers after a turn returns to the
// operator — long enough to register the hand-back, short enough not to become
// permanent chrome.
const IDLE_FLASH_MS = 4000;

// createPresence builds one controller. Thresholds are injectable so a test can
// drive the slow/expire/idle transitions without real-time waits.
export function createPresence({
  slowAfterMs = SLOW_AFTER_MS,
  expireAfterMs = EXPIRE_AFTER_MS,
  idleFlashMs = IDLE_FLASH_MS,
} = {}) {
  let thinking = $state([]); // agent ids with an in-flight turn
  let slow = $state(false); // soften the copy once a turn drags on
  let idle = $state(false); // the brief "Waiting for you" flash
  let slowTimer = null;
  let expireTimer = null;
  let idleTimer = null;

  function clearTurnTimers() {
    clearTimeout(slowTimer);
    clearTimeout(expireTimer);
    slowTimer = null;
    expireTimer = null;
  }

  // armTurnTimers (re)starts the slow + expiry countdowns for the current
  // pending batch: soften the copy at the slow mark, self-clear at the ceiling
  // so an unanswered turn can't strand the indicator.
  function armTurnTimers() {
    clearTurnTimers();
    slowTimer = setTimeout(() => {
      slow = true;
    }, slowAfterMs);
    expireTimer = setTimeout(() => {
      thinking = [];
      slow = false;
      clearTurnTimers();
    }, expireAfterMs);
  }

  // settle installs the remaining pending set; when it empties, the countdowns
  // stop and — if a reply (not a timeout/cancel) emptied it — the brief
  // "Waiting for you" hint flashes to mark the operator's turn.
  function settle(next, { replied = false } = {}) {
    const wasActive = thinking.length > 0;
    thinking = next;
    if (next.length === 0) {
      clearTurnTimers();
      slow = false;
      if (wasActive && replied) {
        idle = true;
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          idle = false;
        }, idleFlashMs);
      }
    }
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

    // add marks personas as working (merging with any already pending) and
    // restarts the countdowns; a fresh turn cancels a lingering idle flash. A
    // call that adds nothing to an empty set is a no-op (a broadcast that
    // addressed no agent shows nothing rather than guessing).
    add(ids) {
      const next = Array.from(
        new Set([...thinking, ...(ids ?? []).filter(Boolean)]),
      );
      if (next.length === 0) return;
      idle = false;
      clearTimeout(idleTimer);
      thinking = next;
      slow = false;
      armTurnTimers();
    },

    // remove clears the given personas. Without `replied` it is a silent
    // backstop (a cancel/error/mid-turn switch) — no idle flash, which belongs
    // only to a turn that actually answered.
    remove(ids, opts) {
      const drop = new Set(ids ?? []);
      settle(
        thinking.filter((id) => !drop.has(id)),
        opts,
      );
    },

    // pruneFrom clears any pending persona whose reply just arrived on a poll
    // tick (the group path, and any agent traffic surfaced after a DM send).
    pruneFrom(messages) {
      if (thinking.length === 0) return;
      const remaining = pruneThinking(thinking, messages);
      if (remaining.length !== thinking.length) {
        settle(remaining, { replied: true });
      }
    },

    // dispose tears the timers down on unmount so a backgrounded tab can't fire
    // a stray state write after the panel is gone.
    dispose() {
      clearTurnTimers();
      clearTimeout(idleTimer);
    },
  };
}
