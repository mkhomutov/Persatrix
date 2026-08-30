<script>
  // The RFC 0039 login surface (enabled-mode exposure amendment §A4):
  // a minimal form rendered by the shell when any console call answers
  // 401 — no new SPA slice, no route. Submitting logs in with the
  // cookie transport, so the session rides the HttpOnly cookie and the
  // token never enters JS; `onsuccess` then reboots the shell, whose
  // /ui/context now reports the verified principal.
  import { login } from "../lib/auth.js";

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
    max-width: 22rem;
    margin: 4rem auto 0;
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
  }
  .login h2 {
    margin: 0;
  }
  .login .hint {
    margin: 0;
    opacity: 0.75;
    font-size: 0.9em;
  }
  .login label {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .login .error {
    margin: 0;
    color: var(--danger);
  }
  .login button {
    align-self: flex-start;
  }
</style>
