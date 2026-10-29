"""Checking a tool call's arguments before the tool runs (RFC 0009 Phase 3).

The model writes a tool call's arguments as JSON. They used to go straight
into the tool as keyword arguments, so a missing or misspelt argument
surfaced as a ``TypeError`` from inside the tool. :func:`validate_arguments`
now reads them first, against what the tool's signature accepts, and returns
either the arguments the tool receives or an :class:`ArgumentRefusal` the
model can act on. Both places a tool runs ask it, through
:func:`agents.tools.tool_list.admit_call`.

It refuses only what fails today (the RFC 0009 Phases 3–4 plan's G-2):

- an argument the tool has no parameter for, or a required one left out,
  each a ``TypeError`` today;
- arguments that are not a JSON object;
- a value that breaks a *narrowing* the tool declared for that parameter
  with ``@tool(narrow=...)``. The narrowing is added to the schema the model
  is offered, and a narrowed parameter is checked against that whole schema,
  its declared type included, so what the model is told is what is checked.
  A built-in tool narrows a parameter only where every value of another type
  already fails inside the tool, with one exception: on Python 3.11 a null
  ``shell_exec`` command read the agent's standard input, and is refused.

It converts a value sent in a lossless other form, so the tool and RFC
0028's later hold see one form: an integer parameter given ``"30"`` or
``30.0`` gets ``30``, and a number parameter given ``1`` or ``"0.5"`` gets
``1.0`` or ``0.5``. A null for an optional parameter means its default,
unless the parameter's annotation takes ``None`` as a value or the tool
narrows the parameter; a null the annotation takes is not limited by a
narrowing. Every other value passes as sent: a tool's own rules, such as
``shell_exec`` clamping its timeout and ``PathValidator`` refusing a path
outside the workspace, stay the tool's. A refusal never carries an
argument's value, so neither the model's reply nor the audit line repeats
what the model sent; the audit line leaves out the names the model made up
too, since a name can carry what a value would.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import jsonschema  # type: ignore[import-untyped]

__all__ = [
    "NARROWING_KEYWORDS",
    "STRING_ONLY",
    "ArgumentRefusal",
    "ArgumentSpec",
    "argument_spec",
    "validate_arguments",
]

#: The JSON Schema keywords a narrowing may use; each limits one value. Every
#: provider's tool schema takes them, Gemini's being the narrowest: it refuses
#: the whole request over ``const``, ``exclusiveMinimum``, ``multipleOf`` or an
#: ``enum`` of anything but strings. ``pattern`` is left out: jsonschema
#: matches it with Python's ``re.search``, where ``$`` also matches before a
#: final newline, so it would admit what the model is told it refuses.
NARROWING_KEYWORDS = frozenset({"type", "enum", "minimum", "maximum", "minLength", "maxLength"})

#: The narrowing of a string parameter that refuses a value of any other type.
STRING_ONLY: Mapping[str, Any] = types.MappingProxyType({"type": "string"})


class _Rule(NamedTuple):
    applies_to: frozenset[str]  # the JSON types of parameter it limits
    wording: str  # how a refusal says a value broke it, without the value


_TEXT, _NUMERIC = frozenset({"string"}), frozenset({"integer", "number"})

# Every narrowing keyword, in one table: a keyword on a parameter of another
# type would be offered to the model and limit nothing.
_RULES: Mapping[str, _Rule] = {
    "type": _Rule(_TEXT | _NUMERIC | {"boolean"}, "must be of type {limit}, not {got}"),
    "enum": _Rule(_TEXT, "must be one of {limit_json}"),
    "minimum": _Rule(_NUMERIC, "must be >= {limit_json}"),
    "maximum": _Rule(_NUMERIC, "must be <= {limit_json}"),
    "minLength": _Rule(_TEXT, "must be at least {limit} character{s}"),
    "maxLength": _Rule(_TEXT, "must be at most {limit} character{s}"),
}

# A refusal names at most this many unknown arguments, each cut to this
# length: the model chooses them, and could send thousands.
_MAX_LISTED = 16
_MAX_NAME = 64

# Integer text that reads back the same, as str(int(text)) == text: "30" and
# "-5", not " 30", "+30", "030", "3_0" or "-0", which a tool may read its own way.
_INTEGER_TEXT = re.compile(r"0|-?[1-9][0-9]*", re.ASCII)

# Decimal text, converted only where it reads back the same, as
# repr(float(text)) == text: "0.5", not "0.50", ".5" or "1e5".
_DECIMAL_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)\.[0-9]+", re.ASCII)

_BY_NAME = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)

# JSON's numbers are finite, but Python's json module reads NaN and Infinity,
# and NaN compares false with every bound. So a narrowed number must be
# finite, or a NaN would pass a minimum and a maximum alike.
_Validator = jsonschema.validators.extend(
    jsonschema.Draft7Validator,
    type_checker=jsonschema.Draft7Validator.TYPE_CHECKER.redefine(
        "number",
        lambda checker, value: jsonschema.Draft7Validator.TYPE_CHECKER.is_type(value, "number")
        and (not isinstance(value, float) or math.isfinite(value)),
    ),
)


@dataclass(frozen=True)
class ArgumentSpec:
    """What a tool's signature accepts, read once when ``@tool`` runs."""

    names: tuple[str, ...]  # every parameter a call may name, in order
    required: tuple[str, ...]
    integers: frozenset[str]  # parameters the tool declares as integers
    numbers: frozenset[str]  # parameters the tool declares as (float) numbers
    takes_none: frozenset[str]  # annotated to take None as a value
    nulls_mean_default: frozenset[str]  # optional, not narrowed, not taking None
    open: bool  # the tool takes **kwargs, so any other name binds as well
    narrowed: frozenset[str]
    validator: Any = field(compare=False, repr=False)  # the narrowings; None if none


@dataclass(frozen=True)
class ArgumentRefusal:
    """Why a call's arguments were refused, naming arguments and never values."""

    input_type: str
    missing: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()  # the first few names, each cut short
    unknown_count: int = 0
    # (argument, the narrowing keyword it broke, its JSON type, the keyword's value)
    invalid: tuple[tuple[str, str, str, Any], ...] = ()
    takes: tuple[str, ...] = ()  # the names the tool takes, for the model's retry

    @property
    def detail(self) -> str:
        """The refusal in one sentence for the model."""
        parts = []
        if self.input_type != "object":
            parts.append(f"arguments must be a JSON object, not {self.input_type}")
        if self.missing:
            plural = "s" if len(self.missing) > 1 else ""
            parts.append(f"missing required argument{plural}: {', '.join(self.missing)}")
        if self.unknown_count:
            plural = "s" if self.unknown_count > 1 else ""
            names = ", ".join(self.unknown)
            if self.unknown_count > len(self.unknown):
                names += f" ... and {self.unknown_count - len(self.unknown)} more"
            takes = f"this tool takes: {', '.join(self.takes)}" if self.takes else (
                "this tool takes no arguments"
            )
            parts.append(f"unknown argument{plural}: {names} ({takes})")
        for argument, rule, got, limit in self.invalid:
            parts.append(f"argument {argument} {_broken(rule, got, limit)}")
        return "; ".join(parts) + ". The tool did not run; fix the arguments and call it again."

    def content(self) -> str:
        """The tool result the model receives, as ASCII-only JSON."""
        return json.dumps({
            "tool_argument_validation_error": self.detail,
            "missing": list(self.missing),
            "unknown": list(self.unknown),
            "invalid": self._invalid(),
        }, ensure_ascii=True)

    def audit_fields(self) -> dict[str, Any]:
        """The fields of the ``tool.arg_invalid`` audit line.

        The unknown names are counted, not listed: the model chose them as
        freely as a value, so a name can carry a secret into the log stream.
        """
        return {
            "input_type": self.input_type,
            "missing_arguments": list(self.missing),
            "unknown_argument_count": self.unknown_count,
            "invalid_arguments": self._invalid(),
        }

    def _invalid(self) -> list[dict[str, str]]:
        return [{"argument": a, "rule": r, "got": g} for a, r, g, _ in self.invalid]


def argument_spec(
    tool_name: str,
    signature: inspect.Signature,
    hints: Mapping[str, Any],
    properties: dict[str, dict[str, Any]],
    narrow: Mapping[str, Mapping[str, Any]] | None,
) -> ArgumentSpec | None:
    """Read what *signature* accepts, merging each narrowing into *properties*.

    *properties* is the offered schema's, one entry per parameter, so a
    narrowing is what the model is told as well as what is checked. A
    narrowing that cannot work raises ``ValueError`` here, when the tool is
    defined, not on its first call. A tool with a positional-only parameter
    cannot take every argument by name, so it gets no spec and its calls are
    checked for shape alone.
    """
    params = signature.parameters
    positional_only = any(p.kind is inspect.Parameter.POSITIONAL_ONLY for p in params.values())
    for name, rule in (narrow or {}).items():
        where = f"tool {tool_name!r}: cannot narrow {name!r}"
        if positional_only or name not in params or params[name].kind not in _BY_NAME:
            raise ValueError(f"{where}: it is not an argument the tool takes by name")
        properties[name].update(_workable(where, rule, properties[name]["type"]))
    if positional_only:
        return None
    by_name = [p for p in params.values() if p.kind in _BY_NAME]
    # Each narrowed parameter is checked against its whole offered schema.
    narrowed = {name: dict(properties[name]) for name in (narrow or {})}
    takes_none = frozenset(
        p.name for p in by_name if _takes_none(hints.get(p.name, p.annotation))
    )
    return ArgumentSpec(
        names=tuple(p.name for p in by_name),
        required=tuple(p.name for p in by_name if p.default is inspect.Parameter.empty),
        integers=frozenset(p.name for p in by_name if properties[p.name]["type"] == "integer"),
        numbers=frozenset(p.name for p in by_name if properties[p.name]["type"] == "number"),
        takes_none=takes_none,
        # A narrowed parameter's null is checked, not replaced: the narrowing
        # says such a value fails inside the tool, which a default would hide.
        nulls_mean_default=frozenset(
            p.name for p in by_name
            if p.default is not inspect.Parameter.empty
            and p.name not in takes_none and p.name not in narrowed
        ),
        open=any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()),
        narrowed=frozenset(narrowed),
        validator=_Validator({"type": "object", "properties": narrowed}) if narrowed else None,
    )


def _workable(where: str, rule: object, own_type: str) -> Mapping[str, Any]:
    """*rule*, if it can narrow a parameter of JSON type *own_type*.

    A narrowing that cannot work raises ``ValueError``: one that refuses every
    value (an empty enum, a minimum over the maximum), one that limits nothing
    (a length on a number), and a bound that is not a finite number, which
    the offered schema could not carry as JSON.
    """
    if not isinstance(rule, Mapping):
        raise ValueError(f"{where}: a narrowing maps keywords to their values")
    if not set(rule) <= NARROWING_KEYWORDS:
        raise ValueError(f"{where} with {sorted(set(rule) - NARROWING_KEYWORDS)}")
    try:
        jsonschema.Draft7Validator.check_schema(dict(rule))
    except jsonschema.SchemaError as exc:
        raise ValueError(f"{where}: {exc.message}") from exc
    if rule.get("type", own_type) != own_type:
        raise ValueError(f"{where} to another type than its own")
    if unfit := sorted(keyword for keyword in rule if own_type not in _RULES[keyword].applies_to):
        raise ValueError(f"{where} with {unfit}: they do not limit a {own_type}")
    if "enum" in rule and not (rule["enum"] and all(isinstance(c, str) for c in rule["enum"])):
        raise ValueError(f"{where}: an enum lists one text value or more")
    if not all(math.isfinite(rule[bound]) for bound in ("minimum", "maximum") if bound in rule):
        raise ValueError(f"{where}: a bound must be a finite number")
    for low, high in (("minimum", "maximum"), ("minLength", "maxLength")):
        if low in rule and high in rule and rule[low] > rule[high]:
            raise ValueError(f"{where}: its {low} is over its {high}, so no value passes")
    return rule


def validate_arguments(
    spec: ArgumentSpec | None, arguments: object,
) -> dict[str, Any] | ArgumentRefusal:
    """The arguments a tool receives for *arguments*, or why they are refused.

    *spec* is the tool's :attr:`ToolDefinition.argument_spec`; with none, the
    arguments are checked for shape alone. *arguments* is left as it is: the
    tool gets a new dict, sharing any nested value with it as ``**`` did.
    """
    if not isinstance(arguments, Mapping):
        return ArgumentRefusal(input_type=_json_type(arguments))
    if spec is None:
        return dict(arguments)
    checked: dict[str, Any] = {}
    unknown: list[object] = []
    for name, value in arguments.items():
        if not isinstance(name, str) or (name not in spec.names and not spec.open):
            unknown.append(name)
        elif value is None and name in spec.nulls_mean_default:
            continue
        elif name in spec.integers:
            checked[name] = _one_integer(value)
        elif name in spec.numbers:
            checked[name] = _one_number(value)
        else:
            checked[name] = value
    missing = tuple(name for name in spec.required if name not in checked)
    invalid = _broken_narrowings(spec, checked) if spec.narrowed else ()
    if unknown or missing or invalid:
        return ArgumentRefusal(
            input_type="object", missing=missing,
            unknown=tuple(_shown(name) for name in unknown[:_MAX_LISTED]),
            unknown_count=len(unknown), invalid=invalid, takes=spec.names,
        )
    return checked


def _broken_narrowings(
    spec: ArgumentSpec, checked: Mapping[str, Any],
) -> tuple[tuple[str, str, str, Any], ...]:
    """Each narrowing a value in *checked* breaks, as ``ArgumentRefusal.invalid``.

    A null the parameter's annotation takes is the tool's own "no value",
    which a narrowing does not limit, so it runs as it did before the check.
    """
    values = {
        name: value for name, value in checked.items()
        if name in spec.narrowed and not (value is None and name in spec.takes_none)
    }
    return tuple(
        (str(error.path[0]), str(error.validator), _json_type(error.instance),
         error.validator_value)
        for error in spec.validator.iter_errors(values) if error.path
    )


def _one_integer(value: Any) -> Any:
    """*value* as an int where it has one lossless int form, else as sent."""
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
        try:
            return int(value)
        except ValueError:  # longer than Python reads integer text
            return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _one_number(value: Any) -> Any:
    """*value* as a float where it has one lossless float form, else as sent.

    An int, or integer text, converts where the float holds it exactly;
    decimal text converts where the float reads back as the same text. A
    bool, and any float, pass as they are.
    """
    if isinstance(value, str):
        if _INTEGER_TEXT.fullmatch(value):
            whole = _one_integer(value)
            return value if isinstance(whole, str) else _one_number(whole)
        if _DECIMAL_TEXT.fullmatch(value) and repr(float(value)) == value:
            return float(value)
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            as_float = float(value)
        except OverflowError:  # past the largest float
            return value
        return as_float if as_float == value else value
    return value


def _takes_none(annotation: Any) -> bool:
    """Whether *annotation* takes ``None`` as a value of its own.

    No annotation, ``Any``, ``object`` and an annotation that could not be
    resolved count as taking it: nothing says otherwise, so null stays null.
    """
    if annotation in (inspect.Parameter.empty, Any, object, None, type(None)):
        return True
    if isinstance(annotation, str):
        return True
    if type(annotation).__name__ == "TypeAliasType":  # ``type X = ...``
        return _takes_none(annotation.__value__)
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return None in typing.get_args(annotation)
    if origin is typing.Annotated:
        return _takes_none(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return any(_takes_none(member) for member in typing.get_args(annotation))
    return False


def _json_type(value: Any) -> str:
    """The JSON type name of *value*, the word a refusal uses for it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number" if math.isfinite(value) else "non-finite number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list | tuple):
        return "array"
    return type(value).__name__


def _shown(name: object) -> str:
    """An unknown argument's name as the model's reply shows it, cut short.

    The model chose it, so it goes through the one log-injection defence,
    which replaces every character that does not print.
    """
    # Imported here: agents.sub_agents imports agents.base, which imports this
    # module through agents.tools.tool_list, so a top-level import is circular.
    from ..sub_agents._log_safety import bounded

    return bounded(name, cap=_MAX_NAME)


def _broken(rule: str, got: str, limit: Any) -> str:
    """How an argument broke the narrowing keyword *rule*, without its value."""
    return _RULES[rule].wording.format(
        limit=limit, got=got, limit_json=json.dumps(limit), s="" if limit == 1 else "s",
    )
