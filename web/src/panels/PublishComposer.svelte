<script>
  // Optional human publish (extracted from ChannelTimeline.svelte to keep the
  // panel under the review-size cap): a clearly-labelled write action so it
  // reads as deliberate, not a search box. The sender is the /ui/context
  // principal — never a free-text field (RFC §F rule 1).
  //
  // It also hosts the `@`-mention typeahead (RFC 0011 mentions over the console):
  // typing `@` opens a menu of the channel's members, picking one lifts `@id `
  // into the draft, and the parent then extracts those tokens into the publish
  // payload's `mentions` array. While the menu is open the arrow keys + Enter/Tab
  // drive it; otherwise keystrokes delegate to the parent's onKeydown (post).
  //
  // content — bound to the panel's draft text.
  // publishing — true while a post is in flight (disables the input).
  // canPublish — whether the Post button is enabled.
  // onSubmit / onKeydown — form submit + textarea keydown handlers (post path).
  // members — the active channel's members ([{ id, … }]); agentsById decorates a
  //   row with a display name; userId is excluded (you don't @-mention yourself).
  import { tick } from "svelte";
  import {
    findActiveMention,
    applyMention,
    mentionCandidates,
  } from "../lib/mentions.js";

  let {
    content = $bindable(),
    publishing,
    canPublish,
    onSubmit,
    onKeydown,
    members = [],
    agentsById = {},
    userId = "",
  } = $props();

  let textareaEl = $state(null);
  let menuOpen = $state(false);
  let candidates = $state([]);
  let activeIndex = $state(0);
  let active = null; // { start, query } of the token under the caret

  const LISTBOX_ID = "publish-mention-listbox";
  const optionId = (i) => `publish-mention-option-${i}`;

  // Caret-only keys edit nothing, so they fire no input event — but they can
  // carry the caret out of (or back into) an `@token`. We resync the menu on
  // their keyup. ArrowUp/Down are deliberately excluded: while the menu is open
  // they drive its selection (handled in keydown), and resyncing would reset the
  // highlighted row out from under the operator.
  const CARET_KEYS = new Set(["ArrowLeft", "ArrowRight", "Home", "End"]);

  function closeMenu() {
    menuOpen = false;
    candidates = [];
    activeIndex = 0;
    active = null;
  }

  // refreshMenu recomputes the typeahead from the textarea's current value +
  // caret: open + filtered when the caret sits in an `@token` that has at least
  // one matching member, closed otherwise.
  function refreshMenu() {
    const caret = textareaEl ? textareaEl.selectionStart : (content?.length ?? 0);
    active = findActiveMention(content, caret);
    if (!active) {
      closeMenu();
      return;
    }
    candidates = mentionCandidates(active.query, members, {
      agentsById,
      exclude: userId,
    });
    if (candidates.length === 0) {
      closeMenu();
      return;
    }
    menuOpen = true;
    activeIndex = 0;
  }

  async function selectCandidate(candidate) {
    if (!active || !candidate) {
      return;
    }
    const caret = textareaEl ? textareaEl.selectionStart : (content?.length ?? 0);
    const next = applyMention(content, active.start, caret, candidate.id);
    content = next.text;
    closeMenu();
    // Restore focus + caret after Svelte writes the new value back to the DOM.
    await tick();
    if (textareaEl) {
      textareaEl.focus();
      textareaEl.setSelectionRange(next.caret, next.caret);
    }
  }

  function onInput() {
    refreshMenu();
  }

  // A caret-only move (arrow keys, Home/End) or a click reposition leaves the
  // open/closed state stale because no input event fires — resync from the new
  // caret. refreshMenu opens, filters, or closes as the caret warrants.
  function onComposerKeyup(event) {
    if (CARET_KEYS.has(event.key)) {
      refreshMenu();
    }
  }

  function onComposerKeydown(event) {
    if (menuOpen) {
      switch (event.key) {
        case "ArrowDown":
          event.preventDefault();
          activeIndex = (activeIndex + 1) % candidates.length;
          return;
        case "ArrowUp":
          event.preventDefault();
          activeIndex =
            (activeIndex - 1 + candidates.length) % candidates.length;
          return;
        case "Enter":
        case "Tab":
          // Take the highlighted member; never let this Enter post the draft.
          event.preventDefault();
          selectCandidate(candidates[activeIndex]);
          return;
        case "Escape":
          event.preventDefault();
          closeMenu();
          return;
        default:
          // A normal keystroke edits the token; the input handler reopens/filters
          // the menu. Don't delegate to the post handler while choosing a member.
          return;
      }
    }
    // Menu closed: the parent owns Enter-to-post (and any other shortcut).
    onKeydown?.(event);
  }

  function mentionLabel(candidate) {
    const name = candidate.name || candidate.id;
    return candidate.role ? `${name} — ${candidate.role}` : name;
  }
</script>

<form class="publish" onsubmit={onSubmit}>
  <label>
    Message
    <textarea
      bind:this={textareaEl}
      bind:value={content}
      rows="2"
      placeholder="Post a message to this channel… (@ to mention, Enter to post, Shift+Enter for a new line)"
      disabled={publishing}
      oninput={onInput}
      onkeydown={onComposerKeydown}
      onkeyup={onComposerKeyup}
      onclick={refreshMenu}
      onblur={closeMenu}
      aria-controls={menuOpen ? LISTBOX_ID : undefined}
      aria-activedescendant={menuOpen ? optionId(activeIndex) : undefined}
      aria-autocomplete="list"
    ></textarea>
  </label>

  {#if menuOpen}
    <ul
      class="mention-menu"
      id={LISTBOX_ID}
      role="listbox"
      aria-label="Channel members"
    >
      {#each candidates as candidate, i (candidate.id)}
        <li
          id={optionId(i)}
          role="option"
          aria-selected={i === activeIndex}
          class:active={i === activeIndex}
          onmousedown={(event) => {
            // Pick before the textarea blur fires — preventDefault keeps focus so
            // the caret restore lands on a still-focused element.
            event.preventDefault();
            selectCandidate(candidate);
          }}
        >
          <span class="mention-id">@{candidate.id}</span>
          <span class="mention-name">{mentionLabel(candidate)}</span>
        </li>
      {/each}
    </ul>
  {/if}

  <button type="submit" disabled={!canPublish}>
    {publishing ? "Posting…" : "Post"}
  </button>
</form>

<style>
  .publish {
    position: relative;
  }
  .mention-menu {
    list-style: none;
    margin: 0;
    padding: 0.25rem 0;
    position: absolute;
    z-index: 10;
    left: 0;
    right: 0;
    max-height: 12rem;
    overflow-y: auto;
    background: var(--surface, #fff);
    border: 1px solid var(--border, #d4d4d8);
    border-radius: 0.375rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  }
  .mention-menu li {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    padding: 0.35rem 0.6rem;
    cursor: pointer;
  }
  .mention-menu li.active {
    background: var(--accent-soft, #eff6ff);
  }
  .mention-id {
    font-weight: 600;
    color: var(--accent, #2563eb);
  }
  .mention-name {
    color: var(--muted, #6b7280);
    font-size: 0.9em;
  }
</style>
