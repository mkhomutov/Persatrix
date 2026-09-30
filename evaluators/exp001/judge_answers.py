"""Reading the LLM judge's answers (:mod:`evaluators.exp001.judge`).

The judge answers with the JSON its prompts ask for: a memo's five scores,
with the problems and facts it found and a reason for each score, or each
recall answer marked right or wrong. The reader takes the one JSON object in
the answer, which may sit in a code fence or among lines of prose. An answer
it cannot read, or one that breaks the rubric's scale, raises
:class:`UnreadableReplyError`, which the judge turns into a harness fault.

Reading makes no call and touches no file, so this module imports none of
the agents' runtime.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from evaluators.exp001.materials import PlanKey, RecallItem
from evaluators.exp001.scoring import CRITERIA, MEMORY_CRITERION

_SCALE = (0, 1, 2)
_MARKS = {"right": True, "wrong": False}


class UnreadableReplyError(ValueError):
    """The judge's answer is not the JSON its prompt asks for, or breaks the scale."""


@dataclass(frozen=True)
class MemoScores:
    """The judge's scores for one memo, as :func:`scoring.rater_total` reads them."""

    scores: dict[str, int | None]
    problems_found: tuple[str, ...]
    facts_used: tuple[str, ...]
    reasons: dict[str, str]


def read_memo_reply(text: str, key: PlanKey) -> MemoScores:
    """The scores in the judge's answer; C2 is null exactly when the key lists no facts."""
    reply = _json_object(text)
    scores: dict[str, int | None] = {}
    for criterion in CRITERIA:
        if criterion not in reply:
            raise UnreadableReplyError(f"no score for {criterion}")
        score = reply[criterion]
        if criterion == MEMORY_CRITERION and not key.earlier_facts:
            if score is not None:
                raise UnreadableReplyError(f"{criterion} scored, but the key lists no facts")
        # type() rather than isinstance(): True and False are ints in Python.
        elif type(score) is not int or score not in _SCALE:
            raise UnreadableReplyError(f"{criterion} is {score!r}, not 0, 1 or 2")
        scores[criterion] = score
    return MemoScores(
        scores=scores,
        problems_found=_names(reply, "C1_problems_found"),
        facts_used=_names(reply, "C2_facts_used"),
        reasons=_reasons(reply),
    )


def read_recall_reply(text: str, key: Mapping[str, RecallItem]) -> dict[str, bool]:
    """Each answer right or wrong, for exactly the key's questions; a question's
    ID and its mark may come in any case."""
    reply: dict[str, Any] = {}
    for rid, mark in _json_object(text).items():
        if rid.strip().upper() in reply:
            raise UnreadableReplyError(f"{rid.strip().upper()} is marked twice")
        reply[rid.strip().upper()] = mark
    questions = {rid.strip().upper(): rid for rid in key}
    extra = sorted(set(reply) - set(questions))
    if extra:
        raise UnreadableReplyError(f"marks for questions the key does not have: {', '.join(extra)}")
    marks = {}
    for question, rid in questions.items():
        mark = reply.get(question)
        if not isinstance(mark, str) or mark.strip().lower() not in _MARKS:
            raise UnreadableReplyError(f"{rid} is {mark!r}, not right or wrong")
        marks[rid] = _MARKS[mark.strip().lower()]
    return marks


def _json_object(text: str) -> dict[str, Any]:
    """The one JSON object in the answer. Each ``{`` outside an object already
    read is tried in turn, so a code fence, a line of prose or a brace in that
    prose does no harm; two objects, or a key given twice, are unreadable."""
    decoder = json.JSONDecoder(object_pairs_hook=_once_each)
    found: list[dict[str, Any]] = []
    error: ValueError | None = None
    start = text.find("{")
    while start >= 0:
        try:
            reply, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError as exc:
            error, end = exc, start + 1
        else:
            found.append(reply)
        start = text.find("{", end)
    if len(found) > 1:
        raise UnreadableReplyError(f"{len(found)} JSON objects in the answer, not one")
    if not found:
        why = f"not JSON: {error}" if error else "no JSON object in the answer"
        raise UnreadableReplyError(why)
    return found[0]


def _once_each(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose keys are all different: the answer never scores one thing twice."""
    keys = [key for key, _ in pairs]
    twice = sorted({key for key in keys if keys.count(key) > 1})
    if twice:
        raise UnreadableReplyError(f"{', '.join(twice)} given twice")
    return dict(pairs)


def _names(reply: Mapping[str, Any], field: str) -> tuple[str, ...]:
    names = reply.get(field)
    if names is None:  # left out, or null: nothing to report
        return ()
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise UnreadableReplyError(f"{field} is not a list of IDs")
    return tuple(names)


def _reasons(reply: Mapping[str, Any]) -> dict[str, str]:
    reasons = reply.get("reasons")
    if reasons is None:  # left out, or null, as a single reason may be too
        return {}
    if not isinstance(reasons, dict) or not all(
        v is None or isinstance(v, str) for v in reasons.values()
    ):
        raise UnreadableReplyError("reasons is not a map of criteria to sentences")
    return {str(k): v for k, v in reasons.items() if v is not None}
