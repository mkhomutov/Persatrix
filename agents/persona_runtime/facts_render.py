"""Render-side text primitives for the facts tier.

Split from :mod:`.facts_section` at the 500-line code cap (the
``dispatch_context`` / ``_migration_registry`` precedent): this module
owns the line/header *text shaping*, :mod:`.facts_section` owns recall,
admission, and budget accounting.

Bounded header template (ISSUE-0116 — v0.3.13 fold-in)
------------------------------------------------------
A fact ``subject`` is LLM-proposed text, and ``render_facts_section``
places it in the persona's own framing position — the
``Known facts about <subject>:`` block header renders OUTSIDE the RFC
0009 ``<external_data>`` quarantine envelope.  The RFC 0026 topic
amendment bounded the *shape* of a stored subject at the write boundary
(``MAX_SUBJECT_CHARS = 120``, control characters and envelope
delimiters rejected — see :mod:`agents.memory.fact_predicates`), but
not the *content* of a short well-formed one: nothing rejects
``atlas. ignore all prior instructions``, and topic seeding (RFC 0049
P1) made the surface reachable from any stimulus that mentions a
stored topic subject.

:func:`bounded_header_subject` is the issue's direction-2 mitigation:
the header renders the subject through a conservative word/char bound
(:data:`HEADER_SUBJECT_MAX_WORDS` / :data:`HEADER_SUBJECT_MAX_CHARS`,
truncation marked with the same ``…`` marker
``memory_budget._truncate_to_token_limit`` uses), so the
model-influenced text in the trusted framing position shrinks from the
120-char write cap to the bounded prefix.  **Header-only**: storage,
the rendered fact rows (:func:`format_fact_line`), recall seeding, and
subject grouping all keep the full canonical form — a truncated header
above an untruncated row is the designed shape, not a mismatch.

Bounds are sized to pass legitimate subjects untouched: person display
names run 1–3 words, UUID participant ids are one 36-char word, and
the extractor is instructed to emit canonical *short* topic names — so
4 words / 48 chars is headroom, not pressure.  Two acknowledged
residuals, both deliberate:

* A subject already inside the bounds renders verbatim — the template
  bounds the *surface*, not the semantics of a short imperative.  The
  principled fix (subject namespaces with per-namespace validation)
  belongs with the future predicate registry — ISSUE-0116 direction 3.
* Two long subjects sharing a bounded prefix render identical headers.
  Grouping keys on the full subject, so the blocks stay distinct and
  their rows disambiguate; a header collision is cosmetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.facts import Fact

__all__ = [
    "HEADER_SUBJECT_MAX_CHARS",
    "HEADER_SUBJECT_MAX_WORDS",
    "bounded_header_subject",
    "format_fact_line",
]


# Conservative render-side bounds on the header's subject slot
# (ISSUE-0116 direction 2).  Strictly tighter than the write-boundary
# ``MAX_SUBJECT_CHARS = 120`` — the drift pin in
# ``test_facts_header_template.py`` asserts the inequality so a future
# write-cap change cannot silently make the template a no-op.  The
# char bound caps the *content*; a truncated header carries the ``…``
# marker on top, so the rendered slot is at most
# ``HEADER_SUBJECT_MAX_CHARS + 1`` characters.
HEADER_SUBJECT_MAX_WORDS: int = 4
HEADER_SUBJECT_MAX_CHARS: int = 48

# Same truncation marker the token-budget truncator uses
# (``memory_budget._truncate_to_token_limit``) so operators reading
# prompt dumps see one convention for "this text was cut".
_TRUNCATION_MARKER = "…"


def bounded_header_subject(subject: str) -> str:
    """Bound *subject* for the ``Known facts about <subject>:`` header.

    Expects the canonical form (single-space separated — every caller
    renders stored subjects, which :func:`~agents.memory.fact_predicates.
    canonicalize_subject` normalized at write time).  Word bound first,
    then the char bound over the surviving words; any cut appends
    ``…``.  In-bounds subjects — the entire legitimate population, by
    sizing — return **byte-identical**, so ``bob`` / ``self`` headers
    render exactly as they did pre-ISSUE-0116 and the landed header
    pins hold unchanged.

    Read-side and total on purpose: like ``canonicalize_subject``, it
    never raises — a pre-amendment over-bound row must stay renderable
    (and erasable), so the bound truncates rather than rejects.
    """
    words = subject.split(" ")
    bounded = " ".join(words[:HEADER_SUBJECT_MAX_WORDS])
    truncated = len(words) > HEADER_SUBJECT_MAX_WORDS
    if len(bounded) > HEADER_SUBJECT_MAX_CHARS:
        # rstrip so a cut landing on a word boundary does not render
        # a dangling space before the marker.
        bounded = bounded[:HEADER_SUBJECT_MAX_CHARS].rstrip()
        truncated = True
    if truncated:
        return bounded + _TRUNCATION_MARKER
    return bounded


def format_fact_line(fact: Fact) -> str:
    """One-line ``- subject predicate object`` render.

    The predicate is intentionally rendered as the raw verb (no
    prettification): the LLM gets the same shape the extractor wrote,
    and operators reading prompt dumps can grep for the canonical
    vocabulary without a translation layer.  The subject is the full
    canonical form — the ISSUE-0116 bound applies to the block header
    only, never to rows.
    """
    return f"- {fact.subject} {fact.predicate} {fact.object}"
