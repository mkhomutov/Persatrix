<script>
  // One governance knob in the Channel settings form (RFC 0050): its label, a
  // provenance badge (overridden here vs inherited fleet default), a one-line
  // hint, the value control, and the inherit/override checkbox. Shared by
  // ChannelSettings (the flat + reasoning knobs) and AutonomousSettings (the
  // RFC 0052 block), which used to carry a copy each.
  //
  // knob       — the descriptor ({key, label, type, hint, options?}).
  // draft      — the parent's reactive draft cell ({inherit, value}); the
  //              controls bind into it, so edits flow to the parent's sparse
  //              patch with no callback.
  // candidates — the member picker's options for `chair` / `convener` knobs
  //              ([{id, name}]).
  let { knob, draft, candidates = [] } = $props();

  const placeholder = { chair: "Select a chair…", convener: "Select a convener…" };
</script>

<li class="knob-row type-{knob.type}" class:overridden={!draft.inherit}>
  <div class="knob-head">
    <span class="knob-label">{knob.label}</span>
    <span class="provenance" class:overridden={!draft.inherit}>
      {draft.inherit ? "Inherited default" : "Overridden on this channel"}
    </span>
  </div>
  {#if knob.hint}
    <p class="knob-hint">{knob.hint}</p>
  {/if}

  <div class="knob-control">
    <!-- The label is shown once in .knob-head above; the control carries it as
         an accessible name (aria-label), not a second visible copy. -->
    {#if knob.type === "bool"}
      <input
        class="value"
        type="checkbox"
        aria-label={knob.label}
        bind:checked={draft.value}
        disabled={draft.inherit}
      />
    {:else if knob.type === "int"}
      <input
        class="value"
        type="number"
        aria-label={knob.label}
        min="0"
        step="1"
        bind:value={draft.value}
        disabled={draft.inherit}
      />
    {:else if knob.type === "enum"}
      <!-- A fixed value set on the knob (reasoning.mode/model/depth). -->
      <select
        class="value"
        aria-label={knob.label}
        bind:value={draft.value}
        disabled={draft.inherit}
      >
        {#each knob.options as opt (opt)}
          <option value={opt}>{opt}</option>
        {/each}
      </select>
    {:else if knob.type === "list"}
      <!-- The agenda: one sub-topic per line. The parent coerces this text
           to/from the `[]string` wire shape (agendaToText/List). -->
      <textarea
        class="value agenda"
        aria-label={knob.label}
        rows="3"
        bind:value={draft.value}
        disabled={draft.inherit}
      ></textarea>
    {:else if knob.type === "chair" || knob.type === "convener"}
      <select
        class="value"
        aria-label={knob.label}
        bind:value={draft.value}
        disabled={draft.inherit}
      >
        <option value="" disabled>{placeholder[knob.type]}</option>
        {#each candidates as cand (cand.id)}
          <option value={cand.id}>{cand.name}</option>
        {/each}
      </select>
    {:else}
      <!-- type === "text": the topic / goal free-text strings. -->
      <input
        class="value"
        type="text"
        aria-label={knob.label}
        bind:value={draft.value}
        disabled={draft.inherit}
      />
    {/if}

    <label class="inherit">
      <input
        type="checkbox"
        bind:checked={draft.inherit}
        aria-label={`Inherit fleet default for ${knob.label}`}
      />
      Inherit fleet default
    </label>
  </div>
</li>
