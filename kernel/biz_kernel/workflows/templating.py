"""Tiny `{{a.b.c}}` templating for SOP args. A string that is one template yields the raw value."""

from __future__ import annotations

import re
from typing import Any

_ONE = re.compile(r"^\{\{\s*([\w.\-]+)\s*\}\}$")
_ANY = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")


class TemplateError(KeyError):
    pass


def lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise TemplateError(path)
    return cur


def render(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        one = _ONE.match(value)
        if one:
            return lookup(ctx, one.group(1))
        return _ANY.sub(lambda m: str(lookup(ctx, m.group(1))), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


_COND = re.compile(r"^(.*?)\s+(==|!=|in)\s+(.*)$")


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def holds(condition: str, ctx: dict[str, Any]) -> bool:
    """Evaluate a `when:` condition: `<template> == x`, `!= x` or `in a,b,c`.

    Templates are rendered first; comparison is on trimmed, case-folded text.
    """
    m = _COND.match(condition.strip())
    if not m:
        raise ValueError(f"bad condition {condition!r}: use '<value> ==|!=|in <value>'")
    left, op, right = (_norm(render(m[1], ctx)), m[2], str(render(m[3], ctx)))
    if op == "in":
        return left in {_norm(r) for r in right.split(",")}
    return (left == _norm(right)) == (op == "==")
