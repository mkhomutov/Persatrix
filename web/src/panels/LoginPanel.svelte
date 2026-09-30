<script>
  // The RFC 0039 login surface (enabled-mode exposure amendment §A4):
  // a minimal form rendered by the shell when any console call answers
  // 401 — no new SPA slice, no route. Submitting logs in with the
  // cookie transport, so the session rides the HttpOnly cookie and the
  // token never enters JS; `onsuccess` then reboots the shell, whose
  // /ui/context now reports the verified principal.
  import { login } from "../lib/auth.js";
  import Icon from "../ui/Icon.svelte";

  let { onsuccess } = $props();
  let username = $state("");
  let password = $state("");
  let error = $state("");
  let busy = $state(false);

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    busy = true;
    error = "";
    try {
      await login(username, password);
      onsuccess?.();
    } catch (e) {
      // The server's own wording ("invalid credentials", "too many
      // login attempts") — it never distinguishes an unknown username
      // from a wrong password, so neither can this line.
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

<form class="login" onsubmit={submit} aria-label="Sign in">
  <div class="login-icon" aria-hidden="true"><Icon name="lock" size={20} /></div>
  <h2>Sign in</h2>
  <p class="hint">
    This orchestrator requires authentication (<code>auth.mode: enabled</code>).
  </p>
  <label>
    Username
    <input
      name="username"
      bind:value={username}
      autocomplete="username"
      required
    />
  </label>
  <label>
    Password
    <input
      name="password"
      type="password"
      bind:value={password}
      autocomplete="current-password"
      required
    />
  </label>
  {#if error}
    <p class="error" role="alert">{error}</p>
  {/if}
  <button type="submit" disabled={busy}>
    {busy ? "Signing in…" : "Sign in"}
  </button>
</form>

<style>
  .login {
    width: min(23rem, calc(100% - 2rem));
    margin: 11vh auto auto;
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    padding: 1.75rem 1.6rem 1.6rem;
    background: var(--surface, #fff);
    border: 1px solid var(--border, #e3e6ec);
    border-radius: var(--radius-lg, 14px);
    box-shadow: var(--shadow-pop, 0 10px 30px rgba(0, 0, 0, 0.12));
  }
  .login-icon {
    width: 2.6rem;
    height: 2.6rem;
    border-radius: 12px;
    display: grid;
    place-items: center;
    color: var(--accent-contrast, #fff);
    background: linear-gradient(140deg, #7a78ff, #4c4ad0);
  }
  .login h2 {
    margin: 0.2rem 0 0;
    font-size: 1.25rem;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .login .hint {
    margin: -0.4rem 0 0.2rem;
    color: var(--text-muted, #5d6576);
    font-size: 0.85rem;
  }
  .login label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text-muted, #5d6576);
  }
  .login input {
    font-weight: 400;
  }
  .login .error {
    margin: 0;
    padding: 0.5rem 0.7rem;
    font-size: 0.82rem;
    color: var(--danger, #c2332b);
    background: var(--danger-soft, #fdeeed);
    border-radius: var(--radius-sm, 8px);
  }
  .login button {
    margin-top: 0.3rem;
    min-height: 2.4rem;
  }
</style>
