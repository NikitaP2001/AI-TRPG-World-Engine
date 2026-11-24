"""What an agent is told when a tool call could not be taken.

A call whose arguments do not fit the tool's own parameters never reaches the
tool: the framework refuses it first, and what the agent then reads is all it has
to correct the call with. That reading is written here — the parameter, what it
must be, what arrived — instead of the framework's own dump of its validation
internals, which names its error classes and its version and leaves the parameter
a bare string in brackets.

A failure that is not about arguments ("the tool raised") is passed on as one
bounded line: those messages are written by this project and say what went wrong.
"""
from __future__ import annotations

from typing import Any, List

# What an argument of a given kind must be.
_EXPECTED = {
    "dict_type": "a dictionary",
    "list_type": "a list",
    "str_type": "text",
    "string_type": "text",
    "int_type": "a whole number",
    "int_parsing": "a whole number",
    "float_type": "a number",
    "float_parsing": "a number",
    "bool_type": "true or false",
    "bool_parsing": "true or false",
    "bytes_type": "bytes",
}

# What a value of a given kind is called, for the other half of the sentence.
_ARRIVED = {
    "dict": "a dictionary",
    "list": "a list",
    "str": "text",
    "int": "a whole number",
    "float": "a number",
    "bool": "true or false",
    "NoneType": "nothing",
}

_LINE_LIMIT = 300


def tool_failure(tool: str, exc: BaseException) -> str:
    """The line an agent is given for one call that could not be taken.

    One part per parameter that did not fit, so a call with two wrong arguments
    is corrected in one reading rather than two.
    """
    name = str(tool or "").strip()
    head = f"{name}: " if name else ""
    problems = _argument_problems(exc)
    if problems:
        return f"ERROR: {head}" + "; ".join(problems)
    return f"ERROR: {head}{_one_line(exc)}"


def _argument_problems(exc: BaseException) -> List[str]:
    """What of the call did not fit, in the words of the parameter."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return []
    try:
        items = errors()
    except Exception:
        return []
    out: List[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        where = ".".join(str(part) for part in (item.get("loc") or ())) or "arguments"
        kind = str(item.get("type") or "")
        if kind == "missing":
            out.append(f"'{where}' is required")
            continue
        expected = _EXPECTED.get(kind)
        if not expected:
            # A rule of the framework's own rather than a kind of value — the
            # framework's sentence is about the argument, so it is kept.
            said = str(item.get("msg") or "").strip().rstrip(".")
            out.append(f"'{where}': {said}" if said else f"'{where}' could not be taken")
            continue
        arrived = _ARRIVED.get(type(item.get("input")).__name__)
        out.append(
            f"'{where}' must be {expected}"
            + (f" ({arrived} was given)" if arrived else "")
        )
    return out


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc or "").split()) or exc.__class__.__name__
    return text if len(text) <= _LINE_LIMIT else text[:_LINE_LIMIT].rstrip() + "…"
