import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import { tick } from "svelte";
import LoginPanel from "./LoginPanel.svelte";

// The RFC 0039 login form (amendment §A4): rendered by the shell on the
// first 401, logs in with the COOKIE transport so the token never
// enters JS. These specs pin the wire shape and the two outcomes; the
// 401-triggered rendering itself is pinned in lib/auth.test.js +
// App-level wiring.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function jsonResponse(body, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(body) };
}

async function fillAndSubmit() {
  await fireEvent.input(screen.getByLabelText(/username/i), {
    target: { value: "alice" },
  });
  await fireEvent.input(screen.getByLabelText(/password/i), {
    target: { value: "correct horse battery" },
  });
  await fireEvent.submit(screen.getByRole("form", { name: /sign in/i }));
}

describe("LoginPanel", () => {
  it("logs in over the cookie transport and reports success", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse({ participant_id: "alice-participant", role: "operator" }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onsuccess = vi.fn();
    render(LoginPanel, { props: { onsuccess } });

    await fillAndSubmit();
    await tick();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/login",
      expect.objectContaining({ method: "POST" }),
    );
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.username).toBe("alice");
    expect(body.password).toBe("correct horse battery");
    expect(body.session_transport).toBe(
      "cookie",
      "the console session must ride the HttpOnly cookie — never a JS-held token",
    );
    expect(onsuccess).toHaveBeenCalled();
  });

  it("shows the server's own wording on a refused login, without success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            { error: "invalid credentials", code: "UNAUTHORIZED" },
            false,
            401,
          ),
        ),
      ),
    );
    const onsuccess = vi.fn();
    render(LoginPanel, { props: { onsuccess } });

    await fillAndSubmit();
    await tick();

    expect(screen.getByRole("alert").textContent).toContain(
      "invalid credentials",
    );
    expect(onsuccess).not.toHaveBeenCalled();
  });
});
