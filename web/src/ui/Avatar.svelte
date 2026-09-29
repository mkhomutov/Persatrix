<script>
  // A participant's avatar: initials on a colour hashed from the id (stable
  // across messages, lists and reloads), with an optional presence dot.
  // Decorative — the name beside it carries the meaning — so it is aria-hidden.
  //
  // id     — hashed for the colour.
  // label  — display text the initials come from ("Ember Owl" → "EO").
  // size   — px; the initials scale with it.
  // status — agent health ("healthy" | "degraded" | "unhealthy" | …); any
  //          other value draws no dot.
  // self   — the operator's own avatar, drawn in the accent colour.
  import { hueForId, initialsFor } from "../lib/format.js";

  let { id = "", label = "", size = 28, status = "", self = false } = $props();

  const DOT = { healthy: "ok", degraded: "warn", unhealthy: "danger" };
  const dot = $derived(DOT[status] ?? "");
</script>

<span
  class="avatar-ui"
  class:self
  style="--h: {hueForId(id)}; --size: {size}px"
  aria-hidden="true"
  >{initialsFor(label || id)}{#if dot}<span class="dot {dot}"></span>{/if}</span
>

<style>
  .avatar-ui {
    position: relative;
    flex: none;
    width: var(--size);
    height: var(--size);
    border-radius: calc(var(--size) * 0.28);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: calc(var(--size) * 0.38);
    font-weight: 700;
    letter-spacing: 0.02em;
    line-height: 1;
    color: light-dark(hsl(var(--h) 48% 30%), hsl(var(--h) 60% 86%));
    background: light-dark(hsl(var(--h) 70% 90%), hsl(var(--h) 32% 27%));
    user-select: none;
  }

  .avatar-ui.self {
    color: var(--accent-contrast);
    background: var(--accent);
  }

  .dot {
    position: absolute;
    right: -2px;
    bottom: -2px;
    width: max(7px, calc(var(--size) * 0.3));
    height: max(7px, calc(var(--size) * 0.3));
    border-radius: 50%;
    border: 2px solid var(--dot-ring, var(--surface));
    background: var(--text-subtle);
  }

  .dot.ok {
    background: var(--ok);
  }

  .dot.warn {
    background: var(--warn);
  }

  .dot.danger {
    background: var(--danger);
  }
</style>
