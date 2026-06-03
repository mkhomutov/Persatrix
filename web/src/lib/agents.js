// Persona/agent display helpers shared by the conversation panel and its
// PersonaPicker (extracted so they render identical option labels and
// chattability rules without the panel re-deriving them — and to keep the panel
// under the review-size cap, the same reason PublishComposer was split out).

// isChattable: task agents (agents.yaml `type: "task"`) run workflow steps and
// never hold a conversation, so a chat turn dead-ends in a timeout. They show in
// the picker but disabled (extends the §A agent DTO); any non-"task" type — incl.
// an unset one from an agent predating the field — stays chattable, so the guard
// can never regress a real conversation.
export function isChattable(agent) {
  return agent?.type !== "task";
}

// agentLabel is the picker's display text: the persona's name, falling back to
// its id when unnamed (matching the server's own display-name fallback in
// chat_handler.go). A non-healthy persona is annotated with its status, since
// only a healthy one can actually reply (the chat route 503s otherwise) — the
// operator sees that before spending a send, not after.
export function agentLabel(agent) {
  const name = agent.name ? agent.name : agent.id;
  // Fold the role into the option so the picker reads as a cast of personas
  // ("Ada — Researcher") rather than a list of bare names (RFC 0048 §A). Role
  // is optional; omit the separator when unset.
  const named = agent.role ? `${name} — ${agent.role}` : name;
  // A task agent's row carries the why ("show but explain") rather than its
  // health — a disabled row can't be sent regardless of status, so the reason
  // it's disabled is the useful annotation.
  if (!isChattable(agent)) {
    return `${named} (task agent — not chattable)`;
  }
  return agent.status && agent.status !== "healthy"
    ? `${named} (${agent.status})`
    : named;
}
