<script>
  // A single-select ARIA listbox (APG pattern) for the sidebar's conversation
  // lists. One tab stop (roving tabindex); arrows move focus without selecting,
  // Home/End jump, Enter/Space pick, and a typed letter jumps to the next row
  // starting with it. Disabled rows stay visible (with the reason in their
  // label) but are skipped by the keyboard and ignore clicks.
  //
  // label       — the listbox's accessible name.
  // items       — the rows, in display order.
  // selectedKey — key of the selected row ("" = none).
  // getKey / getLabel / isDisabled — row accessors; getLabel is the option's
  //               accessible name, so extra visual detail can't blur it.
  // onselect    — called with the picked item.
  // row         — optional snippet (item, { selected }) for the visual row.
  // emptyText   — shown instead of the listbox when there are no items.
  let {
    label,
    items = [],
    selectedKey = "",
    getKey = (item) => item.id,
    getLabel = (item) => item.label ?? item.id,
    isDisabled = () => false,
    onselect,
    row,
    emptyText = "",
  } = $props();

  let listEl = $state(null);
  let focusedKey = $state("");

  const enabledKeys = $derived(
    items.filter((item) => !isDisabled(item)).map((item) => getKey(item)),
  );

  // The row that owns the single tab stop: the one the keyboard last visited,
  // else the selection, else the first enabled row.
  const tabStopKey = $derived.by(() => {
    if (focusedKey && enabledKeys.includes(focusedKey)) return focusedKey;
    if (selectedKey && enabledKeys.includes(selectedKey)) return selectedKey;
    return enabledKeys[0] ?? "";
  });

  function focusKey(key) {
    if (!key || !listEl) return;
    focusedKey = key;
    const el = [...listEl.querySelectorAll('[role="option"]')].find(
      (o) => o.dataset.key === key,
    );
    el?.focus();
    el?.scrollIntoView?.({ block: "nearest" });
  }

  // focusFirst hands focus into the list from outside (the sidebar's filter box
  // on ArrowDown). Returns false when there is nothing to focus.
  export function focusFirst() {
    const key = selectedKey && enabledKeys.includes(selectedKey) ? selectedKey : enabledKeys[0];
    if (!key) return false;
    focusKey(key);
    return true;
  }

  // Keep the selected row in view when the selection changes from elsewhere
  // (the jump box, a just-created channel) in a list longer than the rail.
  $effect(() => {
    const key = selectedKey;
    if (!key || !listEl) return;
    const el = [...listEl.querySelectorAll('[role="option"]')].find(
      (o) => o.dataset.key === key,
    );
    el?.scrollIntoView?.({ block: "nearest" });
  });

  function pick(item) {
    if (isDisabled(item)) return;
    focusedKey = getKey(item);
    onselect?.(item);
  }

  function onKeydown(event, item) {
    const keys = enabledKeys;
    const at = keys.indexOf(getKey(item));
    let next = null;
    switch (event.key) {
      case "ArrowDown":
        next = keys[at === -1 || at >= keys.length - 1 ? 0 : at + 1];
        break;
      case "ArrowUp":
        next = keys[at <= 0 ? keys.length - 1 : at - 1];
        break;
      case "Home":
        next = keys[0];
        break;
      case "End":
        next = keys[keys.length - 1];
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        pick(item);
        return;
      default:
        if (event.key?.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey) {
          next = nextByLetter(event.key, at);
        }
    }
    if (next) {
      event.preventDefault();
      focusKey(next);
    }
  }

  // nextByLetter finds the next enabled row (after the current one, wrapping)
  // whose label starts with the typed letter.
  function nextByLetter(letter, from) {
    const lower = letter.toLowerCase();
    const enabled = items.filter((item) => !isDisabled(item));
    for (let step = 1; step <= enabled.length; step++) {
      const item = enabled[(from + step) % enabled.length];
      if (String(getLabel(item)).toLowerCase().startsWith(lower)) {
        return getKey(item);
      }
    }
    return null;
  }
</script>

{#if items.length === 0}
  {#if emptyText}
    <p class="select-list-empty">{emptyText}</p>
  {/if}
{:else}
  <ul class="select-list" role="listbox" aria-label={label} bind:this={listEl}>
    {#each items as item (getKey(item))}
      {@const key = getKey(item)}
      {@const selected = key === selectedKey}
      {@const disabled = isDisabled(item)}
      <li
        role="option"
        class="select-row"
        class:selected
        class:disabled
        data-key={key}
        aria-label={getLabel(item)}
        aria-selected={selected}
        aria-disabled={disabled ? "true" : undefined}
        tabindex={key === tabStopKey ? 0 : -1}
        onclick={() => pick(item)}
        onkeydown={(event) => onKeydown(event, item)}
      >
        {#if row}
          {@render row(item, { selected, disabled })}
        {:else}
          {getLabel(item)}
        {/if}
      </li>
    {/each}
  </ul>
{/if}
