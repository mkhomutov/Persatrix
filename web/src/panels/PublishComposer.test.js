import { describe, it, expect, vi, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  within,
} from "@testing-library/svelte";
import PublishComposer from "./PublishComposer.svelte";

// The publish composer is a plain textarea PLUS an `@`-mention typeahead over the
// channel's members (RFC 0011 mentions over the console): typing `@` opens a
// member menu, picking one lifts `@id ` into the draft, and while the menu is
// open Enter selects rather than posting. The parent owns the actual post via
// onKeydown/onSubmit — exercised here with spies.
afterEach(cleanup);

const members = [
  { id: "ember-owl" },
  { id: "iron-fox" },
  { id: "nova-sparrow" },
];
const agentsById = {
  "ember-owl": { id: "ember-owl", name: "Ember Owl", role: "Engineering" },
  "iron-fox": { id: "iron-fox", name: "Iron Fox", role: "Infra" },
};

function setup(overrides = {}) {
  const onSubmit = vi.fn((e) => e?.preventDefault?.());
  const onKeydown = vi.fn();
  render(PublishComposer, {
    props: {
      content: "",
      publishing: false,
      canPublish: true,
      onSubmit,
      onKeydown,
      members,
      agentsById,
      userId: "local",
      ...overrides,
    },
  });
  const textarea = screen.getByRole("textbox", { name: /message/i });
  return { textarea, onSubmit, onKeydown };
}

// Drive the textarea like a real edit: set value + caret, then fire input so the
// composer recomputes the active mention from the element's selectionStart.
async function typeInto(textarea, value, caret = value.length) {
  textarea.value = value;
  textarea.selectionStart = caret;
  textarea.selectionEnd = caret;
  await fireEvent.input(textarea);
}

describe("PublishComposer @-mention typeahead", () => {
  it("opens a member menu when the caret enters an @token", async () => {
    const { textarea } = setup();
    await typeInto(textarea, "@");

    const menu = screen.getByRole("listbox");
    const options = within(menu).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      expect.stringContaining("Ember Owl"),
      expect.stringContaining("Iron Fox"),
      // no display name for nova-sparrow → falls back to the id
      expect.stringContaining("nova-sparrow"),
    ]);
  });

  it("filters the menu by the partial query", async () => {
    const { textarea } = setup();
    await typeInto(textarea, "@em");

    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0].textContent).toContain("Ember Owl");
  });

  it("inserts the picked member id and closes the menu", async () => {
    const { textarea } = setup();
    await typeInto(textarea, "your read @em");

    const option = within(screen.getByRole("listbox")).getByRole("option");
    await fireEvent.mouseDown(option);

    expect(textarea.value).toBe("your read @ember-owl ");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("Enter selects the active option instead of posting while the menu is open", async () => {
    const { textarea, onKeydown } = setup();
    await typeInto(textarea, "@em");

    await fireEvent.keyDown(textarea, { key: "Enter" });

    expect(textarea.value).toBe("@ember-owl ");
    expect(onKeydown).not.toHaveBeenCalled();
  });

  it("ArrowDown moves the active option before Enter selects it", async () => {
    const { textarea } = setup();
    await typeInto(textarea, "@");

    await fireEvent.keyDown(textarea, { key: "ArrowDown" }); // ember-owl -> iron-fox
    await fireEvent.keyDown(textarea, { key: "Enter" });

    expect(textarea.value).toBe("@iron-fox ");
  });

  it("delegates Enter to the parent (post) when no menu is open", async () => {
    const { textarea, onKeydown } = setup();
    await typeInto(textarea, "just a message");

    await fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onKeydown).toHaveBeenCalledTimes(1);
  });

  it("Escape closes the menu without disturbing the draft", async () => {
    const { textarea } = setup();
    await typeInto(textarea, "@em");

    await fireEvent.keyDown(textarea, { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(textarea.value).toBe("@em");
  });

  it("never offers the operator's own id", async () => {
    const { textarea } = setup({ userId: "ember-owl" });
    await typeInto(textarea, "@");

    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options.map((o) => o.textContent).join(" ")).not.toContain(
      "Ember Owl",
    );
  });
});
