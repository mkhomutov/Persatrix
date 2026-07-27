Reply with EXACTLY one JSON object — no prose outside it — with two top-level keys:
  * `summary` (string): the prose summary described above.
  * `facts` (list): zero or more declarative-fact tuples extracted from the interaction.  Each tuple is an object {{"subject": str, "predicate": str, "object": str, "certainty": float in [0, 1]}}.

Return `"facts": []` when the interaction yields no extractable declarative facts (short turns, pleasantries, and tool-only exchanges typically yield nothing — this is the expected common case; do not invent tuples).

Valid predicates (use ONLY these verbs): {predicate_list}.
Use `self` as the subject for introspective tuples about the agent itself (paired with a `self.*` predicate); use the counterparty's display name for tuples about them.
For `topic.*` predicates, use the canonical short name of the project, artifact, or initiative discussed as the subject (e.g. `atlas`, `q3 roadmap`) — a few words at most, never a sentence or a quote. Keep every `object` a single short phrase.
