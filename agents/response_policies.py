"""The response-gate policy vocabulary (data, not logic).

Split out of :mod:`agents.response_gate` for the same structural reason
as ``scripts/checks/file_size_allowlist.py``: this is **reference data
whose commentary scales with the disposition vocabulary**, not authored
control flow — every RFC 0030 disposition lands here with its rationale,
and the block was pushing the gate itself toward the 500-line code cap.
The gate re-exports every name, so import paths are unchanged
(``from agents.response_gate import POLICY_ALWAYS`` keeps working); new
code may import from either, and the cross-language drift pins
(``test_cross_language_respond_policy_drift.py``) are path-agnostic
either way.
"""

from __future__ import annotations

from typing import Final

# Policy-string constants pinned as ``Final`` so the gate, the proto
# validator, and the test suite all reference the same values.
POLICY_WHEN_MENTIONED: Final[str] = "when_mentioned"
POLICY_ALWAYS: Final[str] = "always"
POLICY_NEVER: Final[str] = "never"

# Disposition vocabulary (RFC 0030 relevance amendment, v0.3.7). The Go
# config loader normalizes these back to the legacy triple above, so the
# gate normally never sees them on the wire. They are recognized here as
# defence-in-depth: if a disposition value ever reaches the gate
# un-normalized (a hand-edited membership row, a caller that bypasses the
# loader), it is treated as an alias of its legacy equivalent rather than
# falling through to the fail-closed ``unknown_policy`` branch. PR 1 of
# the amendment is otherwise behaviourally inert — the legacy branches are
# unchanged.
POLICY_PARTICIPANT: Final[str] = "participant"
POLICY_ADDRESSED: Final[str] = "addressed"
POLICY_OBSERVER: Final[str] = "observer"

# RFC 0030 Tier B (v0.3.8): `chair` is a low-threshold facilitator — a
# `participant` whose low salience `threshold` lets it clear the relevance
# bid readily. The Go loader normalizes it to `always` on both the membership
# row and the wire `respond_policy`, so the gate normally never sees `chair`;
# it is recognized here as defence-in-depth, the same as the other disposition
# aliases. The chair's distinguishing low `threshold` does not ride
# `respond_policy` — it travels on the separate `memberships.threshold` column
# and the `ChannelMessageEvent.threshold` proto field (PR 2b, landed). The
# chair's behaviour (the low-threshold bid + the inert Layer 5 hooks) lives in
# the Tier B bid stage downstream of this pure gate
# (agents/persona_runtime/salience_gate.py), not here.
POLICY_CHAIR: Final[str] = "chair"

# Disposition → legacy alias map, applied once to the incoming policy so
# every downstream branch reads (and labels metrics with) the canonical
# legacy value. This is the Python mirror of Go's
# ``RespondPolicy.Normalize`` (internal/channels/channels.go) — the two
# encode the same disposition→legacy mapping in different languages and
# must be kept in lockstep; both sides are independently pinned by tests
# (test_response_gate_disposition.py here; the channels package's
# normalization tests there).
_DISPOSITION_ALIASES: Final[dict[str, str]] = {
    POLICY_PARTICIPANT: POLICY_ALWAYS,
    POLICY_CHAIR: POLICY_ALWAYS,
    POLICY_ADDRESSED: POLICY_WHEN_MENTIONED,
    POLICY_OBSERVER: POLICY_NEVER,
}
# PR #252 review N-2: a synthetic policy value used on the
# ``channel.messages.gated`` counter for self-sender suppressions —
# both the DM self-sender and the non-DM defense-in-depth re-check.
# These fires are **routing artifacts**, not user-policy outcomes
# (the orchestrator's ``ChannelRouter`` already filters the sender on
# fanout; the gate re-checks because the cleartext gRPC port cannot be
# trusted to carry a non-spoofed ``sender_id``). Labelling them with
# the configured wire policy (e.g. ``always`` for DMs, or whatever was
# on the membership for groups) would conflate two very different
# operational signals and trick an operator looking at a high
# ``policy=always`` count into chasing a phantom membership-config bug
# when the real cause is the router handing self-messages back to the
# gate. Keeping a distinct label preserves the diagnostic signal
# without polluting the user-policy buckets.
POLICY_DEFENSE_IN_DEPTH: Final[str] = "defense_in_depth"

# RFC 0030 Tier B (v0.3.8): a synthetic ``policy`` label for the
# ``channel.messages.gated`` counter when the suppression came from the
# salience bid (not a user-policy outcome) — the same precedent as
# POLICY_DEFENSE_IN_DEPTH. The member's *effective* wire policy was
# ``always`` (an open-floor admit), but a gated fire labelled ``always`` is,
# by construction, a Tier-A directed-elsewhere drop; the Tier-B bias-to-
# silence suppression is a distinct operational signal that an operator
# breaking down ``gated`` by ``policy`` must be able to tell apart.
POLICY_LOW_SALIENCE: Final[str] = "low_salience"

# A bounded sentinel label for the fail-closed unknown/empty-policy branch.
# The wire-side validator already rejects unknown ``respond_policy`` values, so
# this branch should not fire in production; if it does, the metric ``policy``
# label must stay low-cardinality rather than echo the raw (attacker- or
# bug-supplied) wire string onto ``channel.messages.gated`` — the same bounded-
# label discipline as POLICY_DEFENSE_IN_DEPTH / POLICY_LOW_SALIENCE. The raw
# value is not lost: it is logged at warn (``raw_policy``) for diagnosis.
POLICY_UNKNOWN: Final[str] = "unknown"

# RFC 0030 relevance amendment Tier A (v0.3.7), decision D3 / amendment
# OQ #5 (adopted default): the broadcast sentinel. A message addressed to
# the *room* rather than to specific members carries this reserved token in
# its ``mentions`` list; the directed-elsewhere filter treats its presence
# as "do not suppress" so every ``participant`` reaches the turn. The value
# can never collide with a real participant id — ``validateParticipantID``
# (internal/channels/channels.go) forbids ``@`` — so it is a safe in-band
# sentinel that reuses the existing ``mentions`` plumbing end-to-end with no
# new wire field (PR-plan §"Where the 'everyone' signal comes from", option
# (a)). v0.3.7 wires the sentinel end-to-end on the *consumer + transport*
# side — this gate, the Go candidate set, and a persist-validation exemption
# (internal/channels/sqlite_messages.go) so it survives the wire. The only
# piece deferred to a follow-on is the *producer*: the console composer
# expanding a typed ``@everyone``/``@here`` into the sentinel. Until then the
# open-floor (empty ``mentions``) path already admits all participants, so
# directedness is fixed regardless; an explicit broadcast is an additive
# affordance a programmatic caller can already use. Mirrors Go's
# ``MentionEveryone`` (internal/channels/channels.go); the two must stay in
# lockstep (pinned by ``test_cross_language_respond_policy_drift.py``).
MENTION_EVERYONE: Final[str] = "@everyone"

_DM_CHANNEL_PREFIX: Final[str] = "dm:"
