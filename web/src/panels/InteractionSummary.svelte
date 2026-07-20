<script>
  // The interaction-summary surface (v0.3.8, RFC 0020 §C/§D): when an
  // interaction in the active conversation closes — by end-vote, by the RFC 0030
  // Layer 1 cost ceiling, or by going idle — the persona persists a synthesised
  // summary, and this affordance surfaces it at the foot of the conversation
  // view so a converged brainstorm hands back a readable outcome, not just a
  // stop. Additive: with no closed interaction it renders nothing, so an open
  // conversation's live feed is untouched.
  //
  // The read API is per-agent (each participating persona persists its own
  // summary row), so the surface queries the channel's candidate agents for the
  // active scope and shows the single newest closed interaction. The
  // "[interaction summary unavailable]" sentinel is rendered as an explicit
  // unavailable state, never blanked (SS3).
  import { getClosedInteractions } from "../lib/api.js";
  import {
    pickLatestClosed,
    closeTriggerLabel,
    isSummaryUnavailable,
  } from "../lib/interactions.js";
  import { senderLabel } from "../lib/format.js";

  // scope — the RFC 0020 scope to filter on (the active channel id; for a group
  //   channel the scope IS the `group:` id, scopes.py `scope_for_group`).
  // agentIds — the channel's participating personas to query (a DM's peer, or a
  //   group's members), already free of the human principal.
  // agentsById — id → agent, to render the closed interaction's `participants`
  //   as display names (falls back to the raw id when an agent is unknown).
  // userId — the human principal id, so a participant that is the operator
  //   renders as "You": `participants` is the turn-SENDER set, which includes
  //   the human, not only agents (scopes.py / episode_routing stash
  //   `payload.sender`), so the surface must decode it the same way the message
  //   feed does.
  let { scope = "", agentIds = [], agentsById = {}, userId = "" } = $props();

  // Re-poll cadence for a close that lands while the conversation is open. The
  // feed already head-polls messages; this is the parallel summary refresh,
  // paused while the tab is backgrounded.
  //
  // This refresh is the dominant load on the operator console's shared rate-
  // limit budget: each tick fans out one request PER participant agent (the
  // `ids.map` below), so a group channel issues N requests every REFRESH_MS.
  // Left unbounded it kept saturating the bucket even while the message feed
  // was already backing off a 429, so the "Live updates paused" banner never
  // cleared. The backoff below borrows ConversationFeed's shape — double toward
  // MAX_BACKOFF_MS, reset to REFRESH_MS on success — but narrows the *trigger*:
  // it backs off only on a 429, not on any error as ConversationFeed does. That
  // feed issues a single request, so any failure can fairly slow it; this
  // refresh fans out N requests and already holds-vs-shows on a partial failure
  // (see `refresh`), so a lone flaky agent must not slow the whole set. Only a
  // 429 — actual bucket saturation — should stretch the cadence and relieve the
  // pressure instead of amplifying it.
  const REFRESH_MS = 5000;
  const MAX_BACKOFF_MS = 30000;
  // Single newest closed interaction per agent — the surface only shows one.
  const PER_AGENT_LIMIT = 1;
  // Skip the degenerate single-turn rows (per-event tick/task envelopes) the
  // read API keeps retrievable; the conversation view wants real interactions.
  const MIN_TURNS = 2;

  let record = $state(null);
  let loadToken = 0;
  // Current inter-poll delay; doubles on a rate-limited refresh, resets on a
  // clean one. Read by the rescheduling timer below.
  let backoffMs = REFRESH_MS;

  // refresh fetches each candidate agent's latest closed interaction for the
  // scope and keeps the newest. A token guards against a stale resolution
  // writing after scope/agentIds changed.
  //
  // `reset` distinguishes the two callers, which want opposite failure
  // behaviour:
  //   - reset (a scope / agent change): clear the record up front so the
  //     previous conversation's summary never lingers — no flash while the new
  //     reads are in flight, and nothing to wrongly hold if they fail. `next`
  //     (possibly null) is authoritative.
  //   - poll (the interval, same scope): an INCOMPLETE read (some agent failed)
  //     that yields no record must not flap the affordance away — the failing
  //     agent may be the very one holding the latest summary while a peer simply
  //     has none. Hold the current record in that case only.
  async function refresh({ reset = false } = {}) {
    const ids = (agentIds ?? []).filter(Boolean);
    if (!scope || ids.length === 0) {
      record = null;
      backoffMs = REFRESH_MS;
      return;
    }
    if (reset) {
      record = null;
    }
    const token = ++loadToken;
    const settled = await Promise.allSettled(
      ids.map((id) =>
        getClosedInteractions(id, {
          scope,
          limit: PER_AGENT_LIMIT,
          minTurns: MIN_TURNS,
        }),
      ),
    );
    if (token !== loadToken) {
      return;
    }
    // Back off only on a rate-limit (429) — the bucket is saturated, so doubling
    // the cadence relieves it. Any other partial failure keeps the base cadence
    // and falls through to the existing hold-vs-show logic. ApiError carries the
    // HTTP status (api.js), so inspect the rejection reasons for a 429.
    const rateLimited = settled.some(
      (r) => r.status === "rejected" && r.reason?.status === 429,
    );
    backoffMs = rateLimited
      ? Math.min(backoffMs * 2, MAX_BACKOFF_MS)
      : REFRESH_MS;
    const fulfilled = settled.filter((r) => r.status === "fulfilled");
    const records = fulfilled.flatMap((r) => r.value?.interactions ?? []);
    const next = pickLatestClosed(records);
    // Same-scope poll with a partial failure and nothing to show: hold rather
    // than flap. A complete read (all agents answered) is always authoritative,
    // and a reset already cleared above.
    const incomplete = fulfilled.length < settled.length;
    if (!reset && next === null && incomplete) {
      return;
    }
    record = next;
  }

  // Re-fetch whenever the scope or the candidate agent set changes (a channel
  // switch). Reading both props here registers them as effect dependencies; the
  // async fetch itself is launched fire-and-forget.
  $effect(() => {
    void scope;
    void agentIds;
    refresh({ reset: true });
  });

  // Catch a close that happens while the conversation stays open. A
  // self-rescheduling timer (not a fixed setInterval) so a rate-limited refresh
  // can stretch the next delay via backoffMs; a backgrounded tab skips the
  // fetch and rechecks at the base cadence. One chain for the component
  // lifetime, reading the current props at fire time.
  $effect(() => {
    if (typeof window === "undefined") {
      return;
    }
    let timer = null;
    let cancelled = false;
    function arm(delay) {
      timer = setTimeout(tick, delay);
    }
    async function tick() {
      if (typeof document !== "undefined" && document.hidden) {
        arm(REFRESH_MS);
        return;
      }
      await refresh();
      if (cancelled) {
        return;
      }
      arm(backoffMs);
    }
    arm(REFRESH_MS);
    return () => {
      cancelled = true;
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  });

  const unavailable = $derived(
    record ? isSummaryUnavailable(record.summary) : false,
  );
  const triggerLabel = $derived(
    record ? closeTriggerLabel(record.close_reason) : "",
  );
  // The closed-interaction DTO always carries `participants` as an array (the
  // handler normalises null → []), so a converged brainstorm names who took
  // part. `participants` is the turn-SENDER set — the human principal as well
  // as the agents — so decode each id with the same `senderLabel` the message
  // feed uses: the operator → "You", a known agent → "Name — Role" (a blank
  // name still falls back to the id), an unknown id (e.g. a since-deregistered
  // agent) → the raw id.
  const participantNames = $derived(
    (record?.participants ?? []).map((id) =>
      senderLabel(id, userId, agentsById),
    ),
  );
</script>

{#if record}
  <aside
    class="interaction-summary"
    role="status"
    aria-label="Interaction summary"
  >
    <header class="meta">
      <span class="badge">Conversation {triggerLabel}</span>
      {#if record.turn_count}
        <span class="turns">{record.turn_count} turns</span>
      {/if}
    </header>
    {#if unavailable}
      <p class="unavailable">Summary unavailable for this interaction.</p>
    {:else}
      <p class="summary">{record.summary}</p>
    {/if}
    {#if participantNames.length > 0}
      <p class="participants">
        <span class="participants-label">Participants:</span>
        {participantNames.join(", ")}
      </p>
    {/if}
  </aside>
{/if}

<style>
  .interaction-summary {
    flex: none;
    margin: 0.5rem 0 0;
    padding: 0.6rem 0.8rem;
    border: 1px solid var(--border, #d4d4d8);
    border-left: 3px solid var(--accent, #2563eb);
    border-radius: var(--radius, 6px);
    background: var(--surface-muted, #f4f4f5);
    /* Docked between the timeline and the composer — cap it so a long
       synthesis can't squeeze the live feed out; it scrolls internally. */
    max-height: 12rem;
    overflow-y: auto;
  }
  .meta {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
  }
  .badge {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--accent, #2563eb);
  }
  .turns {
    font-size: 0.75rem;
    color: var(--text-muted, #71717a);
  }
  .summary {
    margin: 0;
    white-space: pre-wrap;
  }
  .unavailable {
    margin: 0;
    font-style: italic;
    color: var(--text-muted, #71717a);
  }
  .participants {
    margin: 0.4rem 0 0;
    font-size: 0.75rem;
    color: var(--text-muted, #71717a);
  }
  .participants-label {
    font-weight: 600;
  }
</style>
